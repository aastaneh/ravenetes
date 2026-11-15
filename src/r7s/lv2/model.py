"""Plain data model for LV2 plugin metadata, independent of how it was parsed."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PortType = Literal["audio", "control", "cv", "midi", "atom", "unknown"]
Direction = Literal["input", "output"]


@dataclass
class ScalePoint:
    label: str
    value: float


@dataclass
class Port:
    index: int
    symbol: str
    name: str
    direction: Direction
    type: PortType
    default: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    properties: list[str] = field(default_factory=list)
    scale_points: list[ScalePoint] = field(default_factory=list)

    @property
    def is_control_input(self) -> bool:
        return self.type == "control" and self.direction == "input"

    @property
    def toggled(self) -> bool:
        return "toggled" in self.properties

    @property
    def integer(self) -> bool:
        return "integer" in self.properties

    @property
    def enumeration(self) -> bool:
        return "enumeration" in self.properties


@dataclass
class Plugin:
    uri: str
    name: str
    bundle: str
    binary: str | None = None
    classes: list[str] = field(default_factory=list)
    ui_types: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)

    @property
    def has_ui(self) -> bool:
        return bool(self.ui_types)

    @property
    def category(self) -> str | None:
        return self.classes[0] if self.classes else None

    def port(self, symbol: str) -> Port | None:
        for p in self.ports:
            if p.symbol == symbol:
                return p
        return None

    def ports_of(self, type_: PortType, direction: Direction) -> list[Port]:
        return [p for p in self.ports if p.type == type_ and p.direction == direction]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["has_ui"] = self.has_ui
        d["category"] = self.category
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plugin:
        ports = [
            Port(
                **{
                    **p,
                    "scale_points": [ScalePoint(**s) for s in p.get("scale_points", [])],
                }
            )
            for p in d.get("ports", [])
        ]
        return cls(
            uri=d["uri"],
            name=d["name"],
            bundle=d["bundle"],
            binary=d.get("binary"),
            classes=list(d.get("classes", [])),
            ui_types=list(d.get("ui_types", [])),
            ports=ports,
        )
