"""Validation diagnostics shared by the loader, validators and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning", "info"]
Path = tuple[str | int, ...]


def format_path(path: Path) -> str:
    out = ""
    for seg in path:
        if isinstance(seg, int):
            out += f"[{seg}]"
        else:
            out += f".{seg}" if out else str(seg)
    return out


@dataclass
class Diagnostic:
    severity: Severity
    message: str
    resource: str | None = None  # "Rig/guitar-rig"
    path: Path = ()
    source: str | None = None
    line: int | None = None  # 1-based
    tier: int = 1

    @property
    def location(self) -> str:
        if self.source is None:
            return ""
        return f"{self.source}:{self.line}" if self.line else self.source

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "tier": self.tier,
            "resource": self.resource,
            "path": format_path(self.path) or None,
            "source": self.source,
            "line": self.line,
            "message": self.message,
        }

    def __str__(self) -> str:
        parts = [p for p in (self.location, self.severity, self.resource) if p]
        where = format_path(self.path)
        msg = f"{where}: {self.message}" if where else self.message
        return "  ".join([*parts, msg])


@dataclass
class Report:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, d: Diagnostic) -> None:
        self.diagnostics.append(d)

    def extend(self, ds) -> None:
        self.diagnostics.extend(ds)

    def count(self, severity: Severity) -> int:
        return sum(1 for d in self.diagnostics if d.severity == severity)

    @property
    def ok(self) -> bool:
        return self.count("error") == 0

    def sorted(self) -> list[Diagnostic]:
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(
            self.diagnostics,
            key=lambda d: (d.source is None, d.source or "", d.line or 0, order[d.severity]),
        )
