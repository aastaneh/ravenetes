"""Backend interface: something that runs one plugin instance as a JACK client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CLIENT_PREFIX = "r7s-"


def client_name(plugin_id: str) -> str:
    return f"{CLIENT_PREFIX}{plugin_id}"


@dataclass(frozen=True)
class InstanceSpec:
    """What a running instance is. A change to any field means a restart."""

    id: str
    uri: str
    bundle: str | None = None
    ui: bool = False

    @property
    def client(self) -> str:
        return client_name(self.id)


class BackendError(Exception):
    pass


class Instance(Protocol):
    spec: InstanceSpec

    @property
    def pid(self) -> int | None: ...
    def alive(self) -> bool: ...
    def exit_code(self) -> int | None: ...
    def set_param(self, symbol: str, value: float) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
    def output_tail(self, n: int = 20) -> list[str]: ...


class Backend(Protocol):
    name: str

    def start(self, spec: InstanceSpec, params: dict[str, float]) -> Instance: ...
    def problems(self) -> list[str]: ...  # environment issues, for status/system-check
    def info(self) -> dict: ...
