"""The reconcile loop: make the running plugins and JACK edges match the store.

One thread owns every side effect (processes, JACK connections). Anything
else (API requests, JACK callbacks) only calls `poke()`.

Policy (docs/DESIGN.md):
- Structure is reconciled continuously: crashed plugins restart with backoff,
  r7s-declared edges removed by someone else come back.
- Parameters are sent when the desired value changes (apply, rig switch).
  `applied` is what the spec last asked for; `live` is what any source
  (spec or MIDI CC) last sent. Only spec changes are compared against
  `applied`, so CC moves and native-UI tweaks persist until the spec changes.
- r7s only removes edges it created.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .backend.base import Backend, BackendError, Instance, InstanceSpec
from .desired import Desired, cc_routes, compute_desired
from .hw.inventory import Hardware
from .hw.pipewire import PipeWireError
from .jackgraph import Edge, Graph, GraphError
from .loader import LoadResult
from .lv2.world import PluginWorld
from .store import Store

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
HW_REFRESH = 10.0  # seconds; also refreshed on any JACK graph change
EPS = 1e-9


@dataclass
class Managed:
    spec: InstanceSpec
    instance: Instance
    applied: dict[str, float]  # last values enforced from the spec
    live: dict[str, float]  # last values sent from any source
    started_at: float


@dataclass
class CrashRecord:
    restarts: int = 0
    next_attempt: float = 0.0
    backoff: float = BACKOFF_START
    last_exit: int | None = None
    last_error: str | None = None
    tail: list[str] = field(default_factory=list)


@dataclass
class Event:
    time: float
    type: str  # plugin | connection | device | reconciler | system
    reason: str
    object: str
    message: str
    count: int = 1

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Reconciler:
    def __init__(
        self,
        store: Store,
        make_backend: Callable[[LoadResult], Backend],
        make_graph: Callable[[Callable[[], None]], Graph],
        make_world: Callable[[LoadResult], PluginWorld],
        hw_snapshot: Callable[[], Hardware],
        clock: Callable[[], float] = time.monotonic,
        backend_key: Callable[[LoadResult], object] = lambda r: None,
        make_cc_router: Callable[[Callable[[str, str, float], None]], object] | None = None,
    ):
        self.store = store
        self._make_backend = make_backend
        self._make_graph = make_graph
        self._make_world = make_world
        self._backend_key = backend_key
        self._hw_snapshot = hw_snapshot
        self.clock = clock

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lock = threading.RLock()  # guards status/events for readers

        self.graph: Graph | None = None
        self.graph_error: str | None = None
        self.backend: Backend | None = None
        self._backend_cfg: object = object()
        self.world: PluginWorld | None = None
        self._world_rev = -1
        self.hw: Hardware | None = None
        self.hw_error: str | None = None
        self._hw_at = -1e9
        self._hw_dirty = True

        self.managed: dict[str, Managed] = {}
        self.crashes: dict[str, CrashRecord] = {}
        self.owned: set[Edge] = set()
        self.desired: Desired | None = None
        self.status: dict = {}
        self.events: collections.deque[Event] = collections.deque(maxlen=500)
        self.passes = 0
        self.cc_router = make_cc_router(self.apply_cc) if make_cc_router else None
        self.cc_problems: list[str] = []
        self.reconciled_revision = -1  # store revision the last finished pass saw

    # --- events ------------------------------------------------------------

    def event(self, type_: str, reason: str, obj: str, message: str) -> None:
        with self.lock:
            last = self.events[-1] if self.events else None
            if last and (last.type, last.reason, last.object, last.message) == (type_, reason, obj, message):
                last.count += 1
                last.time = time.time()
                return
            self.events.append(Event(time.time(), type_, reason, obj, message))
        log.info("%s %s %s: %s", type_, reason, obj, message)

    # --- loop --------------------------------------------------------------

    def poke(self) -> None:
        self._wake.set()

    def _graph_changed(self) -> None:
        self._hw_dirty = True
        self._wake.set()

    def start(self) -> None:
        if self.cc_router is not None:
            self.cc_router.start()
        self._thread = threading.Thread(target=self._run, name="reconciler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                if self._stop.is_set():
                    break
                try:
                    self.reconcile_once()
                except Exception as e:  # never let the loop die
                    log.exception("reconcile failed")
                    self.event("reconciler", "Error", "r7sd", f"reconcile failed: {e}")
        finally:
            # Must happen on this thread: plugins are started with a
            # parent-death signal tied to the thread that spawned them, so
            # they are SIGKILLed the moment this thread exits.
            self._stop_all("daemon shutting down")

    def _stop_all(self, why: str) -> None:
        for pid in list(self.managed):
            self._stop_instance(pid, why)

    def shutdown(self) -> None:
        if self.cc_router is not None:
            self.cc_router.stop()
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        else:
            self._stop_all("daemon shutting down")
        if self.graph:
            self.graph.close()

    # --- one pass ----------------------------------------------------------

    def _ensure_graph(self) -> bool:
        if self.graph is not None and self.graph.alive():
            return True
        if self.graph is not None:
            self.event("system", "JackLost", "jack", "lost the JACK server; reconnecting")
            self.graph = None
            self.owned.clear()
        try:
            self.graph = self._make_graph(self._graph_changed)
            if self.graph_error:
                self.event("system", "JackConnected", "jack", "connected to JACK")
            self.graph_error = None
            return True
        except GraphError as e:
            if self.graph_error != str(e):
                self.event("system", "JackUnavailable", "jack", str(e))
            self.graph_error = str(e)
            return False

    def _refresh_hw(self, now: float) -> None:
        if not self._hw_dirty and now - self._hw_at < HW_REFRESH:
            return
        self._hw_dirty = False
        self._hw_at = now
        try:
            self.hw = self._hw_snapshot()
            self.hw_error = None
        except PipeWireError as e:
            self.hw = None
            self.hw_error = str(e)

    def reconcile_once(self) -> None:
        now = self.clock()
        self.passes += 1
        revision = self.store.revision
        resources = self.store.as_load_result()

        cfg = self._backend_key(resources)
        if self.backend is None or cfg != self._backend_cfg:
            if self.managed:
                self.event("reconciler", "BackendChanged", "r7sd", "backend settings changed; restarting plugins")
                for pid in list(self.managed):
                    self._stop_instance(pid, "backend settings changed")
            self.backend = self._make_backend(resources)
            self._backend_cfg = cfg
            for problem in self.backend.problems():
                self.event("system", "BackendProblem", self.backend.name, problem)
        if self.world is None or self._world_rev != self.store.revision:
            self.world = self._make_world(resources)
            self._world_rev = self.store.revision

        if not self._ensure_graph():
            self._publish(resources, None, {}, set(), revision)
            return
        graph = self.graph
        self._refresh_hw(now)
        ports = graph.ports()
        gens = {s.name: s.generation for s in self.store.list("Rig")}
        desired = compute_desired(resources, self.world, self.hw, self.hw_error, set(ports), gens)
        self.desired = desired

        # 1. notice instances that died on their own
        for pid, m in list(self.managed.items()):
            if not m.instance.alive():
                rc = m.instance.exit_code()
                rec = self.crashes.setdefault(pid, CrashRecord())
                rec.restarts += 1
                rec.last_exit = rc
                rec.tail = m.instance.output_tail()
                rec.next_attempt = now + rec.backoff
                self.event("plugin", "Crashed", f"plugin/{pid}",
                           f"exited with {rc}; restarting in {rec.backoff:g}s")
                rec.backoff = min(rec.backoff * 2, BACKOFF_MAX)
                # Keep edge ownership: JACK dropped the edges with the client,
                # and they're reported as "Restored" when it comes back.
                del self.managed[pid]

        # 2. stop what's no longer wanted, or wanted differently
        for pid, m in list(self.managed.items()):
            want = desired.plugins.get(pid)
            if want is None:
                self._stop_instance(pid, "removed from the active rig" if desired.rig else "no active rig")
            elif want.spec != m.spec:
                self._stop_instance(pid, "plugin definition changed; restarting")
        for pid in list(self.crashes):
            if pid not in desired.plugins:
                del self.crashes[pid]

        # 3. start what's missing
        for pid, want in desired.plugins.items():
            if pid in self.managed:
                continue
            rec = self.crashes.get(pid)
            if rec and now < rec.next_attempt:
                continue
            try:
                inst = self.backend.start(want.spec, want.launch_params())
            except BackendError as e:
                rec = self.crashes.setdefault(pid, CrashRecord())
                rec.last_error = str(e)
                rec.next_attempt = now + rec.backoff
                rec.backoff = min(rec.backoff * 2, BACKOFF_MAX)
                self.event("plugin", "StartFailed", f"plugin/{pid}", str(e))
                continue
            self.managed[pid] = Managed(want.spec, inst, dict(want.params), dict(want.params), now)
            self.event("plugin", "Started", f"plugin/{pid}", f"{want.spec.uri} (pid {inst.pid})")

        # 4. parameters
        for pid, m in self.managed.items():
            want = desired.plugins.get(pid)
            if want is None:
                continue
            for sym, val in want.params.items():
                if sym not in m.applied or abs(m.applied[sym] - val) > EPS:
                    try:
                        m.instance.set_param(sym, val)
                        m.applied[sym] = val
                        m.live[sym] = val
                        self.event("plugin", "ParameterSet", f"plugin/{pid}", f"{sym} = {val:g}")
                    except BackendError as e:
                        self.event("plugin", "ParameterFailed", f"plugin/{pid}", f"{sym}: {e}")
                        break

        # 5. connections
        ports = graph.ports()
        present = graph.connections()
        want_edges: set[Edge] = {(e.src, e.dst) for e in desired.edges if e.resolved}
        for e in desired.edges:
            if not e.resolved:
                continue
            edge = (e.src, e.dst)
            if edge in present:
                self.owned.add(edge)  # declared, so ours to manage from now on
                continue
            if e.src not in ports or e.dst not in ports:
                continue  # plugin still starting; port registration will wake us
            try:
                graph.connect(*edge)
            except GraphError as err:
                self.event("connection", "ConnectFailed", f"{e.raw_from} -> {e.raw_to}", str(err))
                continue
            reason = "Restored" if edge in self.owned else "Connected"
            self.owned.add(edge)
            self.event("connection", reason, f"{e.raw_from} -> {e.raw_to}", f"{edge[0]} -> {edge[1]}")
        for edge in list(self.owned - want_edges):
            if edge in present:
                try:
                    graph.disconnect(*edge)
                    self.event("connection", "Disconnected", f"{edge[0]} -> {edge[1]}", "no longer declared")
                except GraphError as err:
                    self.event("connection", "DisconnectFailed", f"{edge[0]} -> {edge[1]}", str(err))
                    continue
            self.owned.discard(edge)

        # 6. MIDI CC routes (the router thread does the I/O)
        routes, problems = cc_routes(desired)
        if problems != self.cc_problems:
            for msg in set(problems) - set(self.cc_problems):
                self.event("midi", "CcUnmapped", "midi", msg)
        self.cc_problems = problems
        if self.cc_router is not None:
            self.cc_router.update(routes)

        self._publish(resources, desired, graph.ports(), graph.connections(), revision)

    def _stop_instance(self, pid: str, why: str) -> None:
        m = self.managed.pop(pid, None)
        if m is None:
            return
        m.instance.stop()
        prefix = m.spec.client + ":"
        self.owned = {e for e in self.owned if not (e[0].startswith(prefix) or e[1].startswith(prefix))}
        self.event("plugin", "Stopped", f"plugin/{pid}", why)

    # --- status ------------------------------------------------------------

    def _publish(self, resources: LoadResult, desired: Desired | None, ports, present, revision: int) -> None:
        now = self.clock()
        rigs = {}
        for r in resources.of_kind("Rig"):
            stored = self.store.get("Rig", r.name)
            rigs[r.name] = {"phase": "Inactive", "generation": stored.generation if stored else None}
        devices = {}
        if desired is not None:
            for name, res in desired.devices.items():
                devices[name] = {
                    "present": res.present,
                    "device": res.device.description if res.device else None,
                    "capture": res.nodes["capture"].label if "capture" in res.nodes else None,
                    "playback": res.nodes["playback"].label if "playback" in res.nodes else None,
                    "errors": dict(res.errors),
                }
        if desired is not None and desired.rig in rigs:
            plugins = {}
            ready = True
            for pid, want in desired.plugins.items():
                m = self.managed.get(pid)
                rec = self.crashes.get(pid)
                st = {"uri": want.spec.uri, "client": want.spec.client, "restarts": rec.restarts if rec else 0}
                if m is not None:
                    up = all(f"{want.spec.client}:{p.symbol}" in ports
                             for p in (want.plugin.ports if want.plugin else [])
                             if p.type in ("audio", "midi", "cv"))
                    st.update(state="Running" if up else "Starting", pid=m.instance.pid,
                              uptime=round(now - m.started_at, 1))
                    ready &= up
                else:
                    ready = False
                    st["state"] = "CrashLoop" if rec and rec.restarts else "Waiting"
                    if rec:
                        st.update(lastExit=rec.last_exit, lastError=rec.last_error,
                                  retryIn=max(0.0, round(rec.next_attempt - now, 1)), log=rec.tail)
                plugins[pid] = st
            conns = []
            for e in desired.edges:
                c = {"section": e.section, "from": e.raw_from, "to": e.raw_to,
                     "jackFrom": e.src, "jackTo": e.dst}
                if not e.resolved:
                    c.update(state="Unresolved", reason=e.reason)
                    ready = False
                elif (e.src, e.dst) in present:
                    c["state"] = "Connected"
                else:
                    c.update(state="Pending", reason="waiting for ports")
                    ready = False
                conns.append(c)
            phase = "Ready" if ready and not desired.errors else (
                "Degraded" if any(p["state"] == "CrashLoop" for p in plugins.values())
                or any(c["state"] == "Unresolved" for c in conns) or desired.errors else "Progressing")
            rigs[desired.rig].update(
                phase=phase, active=True, observedGeneration=desired.generation,
                plugins=plugins, connections=conns, errors=list(desired.errors),
            )
        status = {
            "jack": {"connected": self.graph is not None and self.graph.alive(), "error": self.graph_error},
            "hardware": {"error": self.hw_error},
            "backend": self.backend.info() if self.backend else None,
            "problems": self.backend.problems() if self.backend else [],
            "activeRig": desired.rig if desired else None,
            "rigs": rigs,
            "devices": devices,
            "ownedConnections": len(self.owned),
            "midi": self._midi_status(),
            "passes": self.passes,
            "revision": revision,
        }
        if self.graph is not None and hasattr(self.graph, "sample_rate"):
            try:
                status["jack"].update(sampleRate=self.graph.sample_rate, bufferSize=self.graph.buffer_size)
            except Exception:
                pass
        with self.lock:
            self.status = status
            self.reconciled_revision = revision

    def apply_cc(self, plugin_id: str, symbol: str, value: float) -> None:
        """Called on the CC router thread. Updates `live` only, never `applied`."""
        m = self.managed.get(plugin_id)
        if m is None or not m.instance.alive():
            return
        m.instance.set_param(symbol, value)
        m.live[symbol] = value

    def _midi_status(self) -> dict:
        r = self.cc_router
        if r is None:
            return {"enabled": False, "problems": list(self.cc_problems)}
        return {
            "enabled": r.stats.error is None,
            "error": r.stats.error,
            "routes": len(r.table.routes),
            "subscribed": [f"{c}:{p}" for c, p in r.stats.subscribed],
            "events": r.stats.events,
            "applied": r.stats.applied,
            "problems": list(self.cc_problems),
        }

    def live_params(self, plugin_id: str) -> dict[str, float] | None:
        """Last values r7s sent to a running instance (includes CC-driven changes)."""
        m = self.managed.get(plugin_id)
        return dict(m.live) if m else None
