"""MIDI CC -> plugin parameter mapping, fed by the ALSA sequencer.

r7sd subscribes its own sequencer port to each mapped device's MIDI outputs,
alongside PipeWire, so it never sits in the realtime JACK graph: a Python
stall delays a knob by milliseconds instead of causing an xrun.

CC floods (a fast knob sweep is ~100 messages/s) are coalesced: per batch
of pending events only the latest value per (plugin, parameter) is sent.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .lv2.model import Port

log = logging.getLogger(__name__)

Address = tuple[int, int]  # (sequencer client, port)
Mode = Literal["linear", "log", "toggle", "enum", "int"]


@dataclass(frozen=True)
class CcRoute:
    source: Address
    channel: int | None  # 0-15 on the wire; None = omni
    cc: int
    plugin: str
    symbol: str
    lo: float
    hi: float
    mode: Mode = "linear"
    points: tuple[float, ...] = ()  # enumeration values, ascending


def route_for(port: Port, source: Address, cc: int, channel: int | None, plugin: str,
              lo: float | None = None, hi: float | None = None) -> CcRoute:
    """Build a route; channel is 1-16 as written in YAML (None = omni)."""
    lo = port.minimum if lo is None else lo
    hi = port.maximum if hi is None else hi
    lo = 0.0 if lo is None else lo
    hi = 1.0 if hi is None else hi
    if port.toggled:
        mode: Mode = "toggle"
    elif port.enumeration and port.scale_points:
        mode = "enum"
    elif port.integer:
        mode = "int"
    elif "logarithmic" in port.properties and lo > 0 and hi > 0:
        mode = "log"
    else:
        mode = "linear"
    points = tuple(sp.value for sp in port.scale_points if lo - 1e-9 <= sp.value <= hi + 1e-9) if mode == "enum" else ()
    return CcRoute(source, None if channel is None else channel - 1, cc, plugin, port.symbol,
                   lo, hi, mode, points)


def scale(r: CcRoute, value: int) -> float:
    t = max(0, min(127, value)) / 127.0
    if r.mode == "toggle":
        return r.hi if value >= 64 else r.lo
    if r.mode == "enum" and r.points:
        return r.points[min(len(r.points) - 1, int(t * len(r.points)))]
    if r.mode == "log":
        return r.lo * math.pow(r.hi / r.lo, t)
    x = r.lo + t * (r.hi - r.lo)
    return float(round(x)) if r.mode == "int" else x


@dataclass(frozen=True)
class CcEvent:
    source: Address
    channel: int  # 0-15
    cc: int
    value: int


class RouteTable:
    """Thread-safe lookup: (source, channel, cc) -> routes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: tuple[CcRoute, ...] = ()
        self._index: dict[tuple[Address, int], list[CcRoute]] = {}

    def set(self, routes: Iterable[CcRoute]) -> bool:
        routes = tuple(sorted(set(routes), key=lambda r: (r.source, r.cc, r.plugin, r.symbol)))
        with self._lock:
            if routes == self._routes:
                return False
            index: dict[tuple[Address, int], list[CcRoute]] = {}
            for r in routes:
                index.setdefault((r.source, r.cc), []).append(r)
            self._routes, self._index = routes, index
            return True

    @property
    def routes(self) -> tuple[CcRoute, ...]:
        return self._routes

    def sources(self) -> set[Address]:
        return {r.source for r in self._routes}

    def match(self, ev: CcEvent) -> list[CcRoute]:
        with self._lock:
            cands = self._index.get((ev.source, ev.cc), ())
            return [r for r in cands if r.channel is None or r.channel == ev.channel]


def coalesce(table: RouteTable, events: Iterable[CcEvent]) -> dict[tuple[str, str], float]:
    """Latest scaled value per (plugin, symbol), in arrival order."""
    out: dict[tuple[str, str], float] = {}
    for ev in events:
        for r in table.match(ev):
            key = (r.plugin, r.symbol)
            out.pop(key, None)  # re-insert so dict order = last-arrival order
            out[key] = scale(r, ev.value)
    return out


# --- sequencer I/O ---------------------------------------------------------


class SeqIO(Protocol):
    address: Address

    def subscribe(self, source: Address) -> None: ...
    def unsubscribe(self, source: Address) -> None: ...
    def read(self, timeout: float) -> list[CcEvent]: ...
    def close(self) -> None: ...


class AlsaSeqIO:
    """r7sd's sequencer client with one input port for CCs."""

    def __init__(self, name: str = "r7sd"):
        from alsa_midi import PortCaps, PortType, SequencerClient  # noqa: PLC0415

        self._seq = SequencerClient(name)
        self._port = self._seq.create_port(
            "cc-in", caps=PortCaps.WRITE | PortCaps.SUBS_WRITE,
            type=PortType.MIDI_GENERIC | PortType.APPLICATION,
        )
        self.address = (self._seq.client_id, self._port.port_id)

    def subscribe(self, source: Address) -> None:
        self._seq.subscribe_port(source, self.address)

    def unsubscribe(self, source: Address) -> None:
        self._seq.unsubscribe_port(source, self.address)

    def read(self, timeout: float) -> list[CcEvent]:
        from alsa_midi import ControlChangeEvent  # noqa: PLC0415

        out = []
        ev = self._seq.event_input(timeout=timeout)
        while ev is not None:
            if isinstance(ev, ControlChangeEvent) and ev.source is not None:
                out.append(CcEvent((ev.source.client_id, ev.source.port_id), ev.channel, ev.param, ev.value))
            ev = self._seq.event_input(timeout=0) if self._seq.event_input_pending(fetch_sequencer=False) else None
        return out

    def close(self) -> None:
        self._seq.close()


@dataclass
class RouterStats:
    events: int = 0
    applied: int = 0
    subscribed: list[Address] = field(default_factory=list)
    error: str | None = None


class CcRouter:
    """Owns the sequencer client on its own thread; the reconciler only calls
    update(routes). Each mapped value goes to sink(plugin, symbol, value)."""

    def __init__(self, sink: Callable[[str, str, float], None],
                 open_seq: Callable[[], SeqIO] = AlsaSeqIO, poll: float = 0.2):
        self.sink = sink
        self._open = open_seq
        self._poll = poll
        self.table = RouteTable()
        self.stats = RouterStats()
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribed: set[Address] = set()
        self._failed: dict[Address, str] = {}

    def update(self, routes: Iterable[CcRoute]) -> None:
        if self.table.set(routes):
            self._dirty.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cc-router", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _sync_subscriptions(self, io: SeqIO) -> None:
        want = self.table.sources()
        for src in sorted(self._subscribed - want):
            try:
                io.unsubscribe(src)
            except Exception as e:  # source may have vanished with the device
                log.debug("unsubscribe %s: %s", src, e)
            self._subscribed.discard(src)
        for src in sorted(want - self._subscribed):
            try:
                io.subscribe(src)
                self._subscribed.add(src)
                self._failed.pop(src, None)
                log.info("cc: listening to sequencer port %d:%d", *src)
            except Exception as e:
                if self._failed.get(src) != str(e):
                    log.warning("cc: cannot subscribe to %d:%d: %s", *src, e)
                self._failed[src] = str(e)
        self.stats.subscribed = sorted(self._subscribed)

    def deliver(self, events: list[CcEvent]) -> None:
        self.stats.events += len(events)
        for (plugin, symbol), value in coalesce(self.table, events).items():
            try:
                self.sink(plugin, symbol, value)
                self.stats.applied += 1
            except Exception as e:
                log.debug("cc: %s.%s: %s", plugin, symbol, e)

    def _run(self) -> None:
        try:
            io = self._open()
        except Exception as e:
            self.stats.error = f"ALSA sequencer unavailable: {e}"
            log.warning("cc: %s; MIDI CC mapping disabled", self.stats.error)
            return
        self.stats.error = None
        try:
            while not self._stop.is_set():
                if self._dirty.is_set() or self._failed:
                    self._dirty.clear()
                    self._sync_subscriptions(io)
                events = io.read(self._poll)
                if events:
                    self.deliver(events)
        finally:
            io.close()
