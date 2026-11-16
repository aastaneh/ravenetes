"""In-memory backend for tests: instances register their ports on a FakeGraph."""

from __future__ import annotations

from ..jackgraph import FakeGraph
from ..lv2.world import PluginWorld
from .base import BackendError, InstanceSpec


class FakeInstance:
    _next_pid = 10000

    def __init__(self, backend: FakeBackend, spec: InstanceSpec, params: dict[str, float]):
        self.backend = backend
        self.spec = spec
        self.launch_params = dict(params)
        self.target = spec.preset_path or spec.uri
        self.params = dict(params)
        self.sets: list[tuple[str, float]] = []
        self._rc: int | None = None
        FakeInstance._next_pid += 1
        self._pid = FakeInstance._next_pid
        plugin = backend.world.get(spec.uri)
        ports = {}
        if plugin is not None:
            for p in plugin.ports:
                if p.type in ("audio", "cv"):
                    ports[p.symbol] = "audio"
                elif p.type == "midi":
                    ports[p.symbol] = "midi"
        backend.graph.add_client(spec.client, ports)

    @property
    def pid(self) -> int | None:
        return self._pid

    def alive(self) -> bool:
        return self._rc is None

    def exit_code(self) -> int | None:
        return self._rc

    def set_param(self, symbol: str, value: float) -> None:
        if not self.alive():
            raise BackendError(f"{self.spec.client} is not running")
        self.params[symbol] = value
        self.sets.append((symbol, value))

    def crash(self, rc: int = -11) -> None:
        self._rc = rc
        self.backend.graph.remove_client(self.spec.client)

    def stop(self, timeout: float = 2.0) -> None:
        if self.alive():
            self._rc = 0
            self.backend.graph.remove_client(self.spec.client)
        self.backend.stopped.append(self.spec.id)

    def output_tail(self, n: int = 20) -> list[str]:
        return [] if self.alive() else [f"exited with {self._rc}"]


class FakeBackend:
    name = "fake"

    def __init__(self, world: PluginWorld, graph: FakeGraph):
        self.world = world
        self.graph = graph
        self.instances: list[FakeInstance] = []
        self.stopped: list[str] = []
        self.fail_start: set[str] = set()

    def start(self, spec: InstanceSpec, params: dict[str, float]) -> FakeInstance:
        if spec.id in self.fail_start:
            raise BackendError(f"cannot start {spec.id}")
        inst = FakeInstance(self, spec, params)
        self.instances.append(inst)
        return inst

    def problems(self) -> list[str]:
        return []

    def info(self) -> dict:
        return {"backend": self.name}

    def latest(self, plugin_id: str) -> FakeInstance:
        return [i for i in self.instances if i.spec.id == plugin_id][-1]
