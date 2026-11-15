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
from .eventlog import EventLog
from .store import Store

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
HW_REFRESH = 10.0
XRUN_CHANGE_WINDOW = 1.5  # seconds after a structural change  # seconds; also refreshed on any JACK graph change
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
        eventlog: EventLog | None = None,
        make_sink: Callable[[str], object] | None = None,
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
        self.log = eventlog or EventLog()
        self.log._rig = lambda: self.desired.rig if self.desired else None
        self.passes = 0
        self.xruns_total = 0
        self.rig_xruns: dict[str, int] = {}
        self.cpu_load: float | None = None
        self._sample_rate: int | None = None
        self._device_present: dict[str, bool] = {}
        self._last_rig: str | None = None
        self._last_change = -1e9  # clock() of the last plugin start/stop or re-cable
        self.log_cc_values = False  # Config spec.log.midiCC
        self._make_sink = make_sink
        self._log_backend: str | None = None
        self.cc_router = make_cc_router(self.apply_cc) if make_cc_router else None
        self.cc_problems: list[str] = []
        self.reconciled_revision = -1  # store revision the last finished pass saw

    # --- events ------------------------------------------------------------

    @property
    def events(self):
        """Recent events, newest last (consecutive duplicates folded)."""
        return self.log.recent

    def event(self, type_: str, reason: str, obj: str, message: str, **fields) -> None:
        self.log.emit(type_, reason, obj, message, **fields)

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
        self._apply_log_config(resources)
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
        self._note_rig_change(desired)
        self.desired = desired
        self._note_devices(desired)
        self._note_audio_engine(graph)

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
            self._last_change = now
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
                self.event("connection", "ConnectFailed", f"connection/{e.raw_from}->{e.raw_to}", str(err))
                continue
            reason = "Restored" if edge in self.owned else "Connected"
            self._last_change = self.clock()
            self.owned.add(edge)
            self.event("connection", reason, f"connection/{e.raw_from}->{e.raw_to}", f"{edge[0]} -> {edge[1]}")
        for edge in list(self.owned - want_edges):
            if edge in present:
                try:
                    graph.disconnect(*edge)
                    self.event("connection", "Disconnected", f"connection/{edge[0]}->{edge[1]}", "no longer declared")
                except GraphError as err:
                    self.event("connection", "DisconnectFailed", f"connection/{edge[0]}->{edge[1]}", str(err))
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
        self._last_change = self.clock()
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
                xruns=self.rig_xruns.get(desired.rig, 0),
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
            "xruns": self.xruns_total,
            "cpuLoad": self.cpu_load,
            "log": self.log.backend,
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

    def _apply_log_config(self, resources: LoadResult) -> None:
        cfgs = resources.of_kind("Config")
        spec = cfgs[0].resource.spec.log if cfgs else None
        self.log_cc_values = bool(spec and spec.midi_cc)
        want = spec.backend if spec else "auto"
        if self._make_sink is None:
            return
        if self._log_backend is None:
            self._log_backend = want  # the daemon opened the sink for this setting
            return
        if want != self._log_backend:
            old = self.log.backend
            sink = self._make_sink(want)
            with self.log._lock:
                for s in self.log.sinks:
                    s.close()
                self.log.sinks = [sink]
            self._log_backend = want
            self.event("system", "LogBackendChanged", "r7sd", f"event log: {old} -> {sink.name}")

    # --- observations that only produce events -------------------------------

    def _note_rig_change(self, desired: Desired) -> None:
        if desired.rig != self._last_rig:
            if desired.rig:
                self.event("system", "RigActivated", f"rig/{desired.rig}",
                           f"active rig is now {desired.rig}" + (f" (was {self._last_rig})" if self._last_rig else ""))
            else:
                self.event("system", "RigDeactivated", f"rig/{self._last_rig}", "no active rig")
            self._last_rig = desired.rig

    def _note_devices(self, desired: Desired) -> None:
        for name, res in desired.devices.items():
            was = self._device_present.get(name)
            if was is None:
                self._device_present[name] = res.present
                if not res.present:
                    self.event("device", "DeviceDisconnected", f"device/{name}",
                               res.errors.get("device", "not present"))
                continue
            if res.present != was:
                self._device_present[name] = res.present
                if res.present:
                    self.event("device", "DeviceConnected", f"device/{name}", res.label or name)
                else:
                    self.event("device", "DeviceDisconnected", f"device/{name}",
                               res.errors.get("device", "not present"))
        for name in list(self._device_present):
            if name not in desired.devices:
                del self._device_present[name]

    def _note_audio_engine(self, graph: Graph) -> None:
        sr = getattr(graph, "sample_rate", None)
        try:
            sr = sr() if callable(sr) else sr
        except Exception:
            sr = None
        if sr and self._sample_rate and sr != self._sample_rate:
            self.event("device", "SampleRateChanged", "jack", f"{self._sample_rate} Hz -> {sr} Hz",
                       old=self._sample_rate, new=sr)
        if sr:
            self._sample_rate = sr
        if hasattr(graph, "cpu_load"):
            try:
                self.cpu_load = round(float(graph.cpu_load()), 2)
            except Exception:
                self.cpu_load = None
        drain = getattr(graph, "drain_xruns", None)
        xruns = drain() if drain else []
        if not xruns:
            return
        rig = self._last_rig
        # PipeWire reconfigures its graph when clients come and go, which
        # often costs one cycle; flag those so they aren't read as overload.
        change = self.clock() - self._last_change < XRUN_CHANGE_WINDOW
        note = ", during a graph change" if change else ""
        self.xruns_total += len(xruns)
        if rig:
            self.rig_xruns[rig] = self.rig_xruns.get(rig, 0) + len(xruns)
        # One event per xrun, unless it's a burst: then one summary event.
        if len(xruns) <= 3:
            for t, delay in xruns:
                self.event("xrun", "Xrun", f"rig/{rig}" if rig else "jack",
                           f"xrun (delay {delay:.0f} us, DSP load {self.cpu_load}%{note})",
                           delay_usecs=round(delay), cpu_load=self.cpu_load, at=round(t, 6),
                           graph_change=change or None)
        else:
            worst = max(d for _, d in xruns)
            self.event("xrun", "Xrun", f"rig/{rig}" if rig else "jack",
                       f"{len(xruns)} xruns (worst delay {worst:.0f} us, DSP load {self.cpu_load}%{note})",
                       count=len(xruns), delay_usecs=round(worst), cpu_load=self.cpu_load,
                       graph_change=change or None)

    def apply_cc(self, plugin_id: str, symbol: str, value: float) -> None:
        """Called on the CC router thread. Updates `live` only, never `applied`."""
        m = self.managed.get(plugin_id)
        if m is None or not m.instance.alive():
            return
        m.instance.set_param(symbol, value)
        m.live[symbol] = value
        if self.log_cc_values:
            self.event("midi", "CcValue", f"plugin/{plugin_id}", f"{symbol} = {value:g}",
                       symbol=symbol, value=round(value, 6))

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
