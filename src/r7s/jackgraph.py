"""The JACK graph as r7sd sees it: ports, connections, change notifications."""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

Edge = tuple[str, str]  # (output port, input port)


class GraphError(Exception):
    pass


class Graph(Protocol):
    def alive(self) -> bool: ...
    def ports(self) -> dict[str, str]: ...  # name -> "audio" | "midi"
    def connections(self) -> set[Edge]: ...
    def connect(self, src: str, dst: str) -> None: ...
    def disconnect(self, src: str, dst: str) -> None: ...
    def close(self) -> None: ...


class JackGraph:
    """A JACK client that only observes and patches; it has no ports itself.

    JACK callbacks run on JACK's thread and must not call back into JACK, so
    they only invoke `on_change` (which should just set an event).
    """

    def __init__(self, name: str = "r7sd", on_change: Callable[[], None] | None = None):
        try:
            import jack  # noqa: PLC0415  (lazy: libjack is only needed by r7sd)
        except OSError as e:
            raise GraphError(f"libjack not loadable: {e}") from e
        self._jack = jack
        self._on_change = on_change or (lambda: None)
        self._dead = threading.Event()
        self._xruns: collections.deque[tuple[float, float]] = collections.deque(maxlen=10000)
        self._rate: int | None = None
        try:
            self.client = jack.Client(name, no_start_server=True)
        except jack.JackOpenError as e:
            raise GraphError(f"cannot connect to the JACK server ({e}); is PipeWire/jackd running?") from e

        def changed(*_):
            self._on_change()

        def shutdown(status, reason):
            self._dead.set()
            self._on_change()

        def xrun(delayed_usecs):
            # Notification thread, not RT; still no JACK calls allowed here.
            self._xruns.append((time.time(), float(delayed_usecs)))
            self._on_change()

        def samplerate(rate):
            self._rate = int(rate)
            self._on_change()

        self.client.set_port_registration_callback(changed, only_available=False)
        self.client.set_port_connect_callback(changed, only_available=False)
        self.client.set_client_registration_callback(changed)
        self.client.set_shutdown_callback(shutdown)
        self.client.set_xrun_callback(xrun)
        self.client.set_samplerate_callback(samplerate)
        self.client.activate()

    @property
    def sample_rate(self) -> int:
        return self._rate or self.client.samplerate

    def cpu_load(self) -> float:
        return self.client.cpu_load()

    def drain_xruns(self) -> list[tuple[float, float]]:
        """(epoch time, delay in us) of xruns since the last call."""
        out = []
        while self._xruns:
            out.append(self._xruns.popleft())
        return out

    @property
    def buffer_size(self) -> int:
        return self.client.blocksize

    def alive(self) -> bool:
        return not self._dead.is_set()

    def ports(self) -> dict[str, str]:
        return {p.name: ("midi" if p.is_midi else "audio") for p in self.client.get_ports()}

    def connections(self) -> set[Edge]:
        edges: set[Edge] = set()
        for p in self.client.get_ports(is_output=True):
            for q in self.client.get_all_connections(p):
                edges.add((p.name, q.name))
        return edges

    def connect(self, src: str, dst: str) -> None:
        try:
            self.client.connect(src, dst)
        except self._jack.JackErrorCode as e:
            if "already" not in str(e).lower():
                raise GraphError(f"connect {src} -> {dst}: {e}") from e

    def disconnect(self, src: str, dst: str) -> None:
        try:
            self.client.disconnect(src, dst)
        except self._jack.JackErrorCode as e:
            raise GraphError(f"disconnect {src} -> {dst}: {e}") from e

    def close(self) -> None:
        try:
            self.client.deactivate()
            self.client.close()
        except Exception as e:  # already gone
            log.debug("closing JACK client: %s", e)


class FakeGraph:
    """In-memory JACK graph for tests."""

    def __init__(self, ports: dict[str, str] | None = None):
        self._ports: dict[str, str] = dict(ports or {})
        self.edges: set[Edge] = set()
        self.log: list[tuple[str, str, str]] = []
        self.dead = False
        self.sample_rate = 48000
        self.load = 12.5
        self.xruns: list[tuple[float, float]] = []

    def cpu_load(self) -> float:
        return self.load

    def drain_xruns(self) -> list[tuple[float, float]]:
        out, self.xruns = self.xruns, []
        return out

    def alive(self) -> bool:
        return not self.dead

    def add_client(self, client: str, ports: dict[str, str]) -> None:
        for p, t in ports.items():
            self._ports[f"{client}:{p}"] = t

    def remove_client(self, client: str) -> None:
        prefix = f"{client}:"
        self._ports = {k: v for k, v in self._ports.items() if not k.startswith(prefix)}
        self.edges = {e for e in self.edges if not (e[0].startswith(prefix) or e[1].startswith(prefix))}

    def ports(self) -> dict[str, str]:
        return dict(self._ports)

    def connections(self) -> set[Edge]:
        return set(self.edges)

    def connect(self, src: str, dst: str) -> None:
        if src not in self._ports or dst not in self._ports:
            raise GraphError(f"connect {src} -> {dst}: no such port")
        self.edges.add((src, dst))
        self.log.append(("connect", src, dst))

    def disconnect(self, src: str, dst: str) -> None:
        self.edges.discard((src, dst))
        self.log.append(("disconnect", src, dst))

    def close(self) -> None:
        pass
