"""Structured event log: systemd journal, or an append-only JSON-lines file.

Every event has a type (xrun | plugin | connection | device | midi |
reconciler | system), a reason (CamelCase, like Kubernetes events), an
object ("plugin/amp", "device/scarlett", ...), a message, a syslog priority,
the active rig, and optional extra fields.

The journal is written with journald's native datagram protocol, so no
libsystemd binding is needed. Fields become R7S_TYPE, R7S_REASON, R7S_OBJECT,
R7S_RIG and R7S_<FIELD>, under SYSLOG_IDENTIFIER=r7sd.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

JOURNAL_SOCKET = "/run/systemd/journal/socket"
IDENTIFIER = "r7sd"

PRIORITIES = {"emerg": 0, "alert": 1, "crit": 2, "err": 3, "warning": 4, "notice": 5, "info": 6, "debug": 7}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}

# Default priority per reason; anything else is info.
REASON_PRIORITY = {
    **dict.fromkeys(["Crashed", "StartFailed", "ConnectFailed", "DisconnectFailed", "ParameterFailed",
                     "Error", "JackLost", "JackUnavailable", "UiUnavailable"], 3),
    **dict.fromkeys(["Xrun", "CcUnmapped", "BackendProblem", "DeviceDisconnected", "ApplyRejected"], 4),
    **dict.fromkeys(["Applied", "Deleted", "RigActivated", "RigDeactivated", "DeviceConnected",
                     "SampleRateChanged", "Started", "Stopping", "UiListening"], 5),
    **dict.fromkeys(["ParameterSet", "CcValue", "Hotplug"], 7),
}
# "Started"/"Stopped" for plugins are routine; only the daemon's own are notices.
TYPE_REASON_PRIORITY = {("plugin", "Started"): 6, ("plugin", "Stopped"): 6}


def priority_for(type_: str, reason: str) -> int:
    return TYPE_REASON_PRIORITY.get((type_, reason), REASON_PRIORITY.get(reason, 6))


def parse_priority(s: str | int) -> int:
    if isinstance(s, int) or str(s).isdigit():
        n = int(s)
        if 0 <= n <= 7:
            return n
    elif str(s).lower() in PRIORITIES:
        return PRIORITIES[str(s).lower()]
    raise ValueError(f"unknown priority {s!r}; use one of {', '.join(PRIORITIES)} or 0-7")


@dataclass
class Record:
    time: float  # epoch seconds
    type: str
    reason: str
    object: str
    message: str
    priority: int = 6
    rig: str | None = None
    fields: dict = field(default_factory=dict)
    count: int = 1  # consecutive identical events folded together (memory view only)

    def to_dict(self) -> dict:
        d = {
            "time": self.time, "type": self.type, "reason": self.reason, "object": self.object,
            "message": self.message, "priority": PRIORITY_NAMES.get(self.priority, str(self.priority)),
            "rig": self.rig, "count": self.count,
        }
        if self.fields:
            d["fields"] = dict(self.fields)
        return d

    def to_json_line(self) -> str:
        d = self.to_dict()
        d.pop("count")
        d["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.time)) + f".{int(self.time % 1 * 1e6):06d}"
        return json.dumps(d, separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, d: dict) -> Record:
        pr = d.get("priority", 6)
        return cls(
            time=float(d["time"]), type=d.get("type", ""), reason=d.get("reason", ""),
            object=d.get("object", ""), message=d.get("message", ""),
            priority=parse_priority(pr) if pr is not None else 6,
            rig=d.get("rig"), fields=dict(d.get("fields") or {}),
        )


class Sink(Protocol):
    name: str

    def write(self, r: Record) -> None: ...
    def close(self) -> None: ...


# --- journald -----------------------------------------------------------------


def _field(key: str, value) -> bytes:
    k = key.encode()
    v = str(value).encode("utf-8", "replace")
    if b"\n" in v:
        # Binary-safe form: KEY\n<le64 length><value>\n
        return k + b"\n" + struct.pack("<Q", len(v)) + v + b"\n"
    return k + b"=" + v + b"\n"


def _field_name(name: str) -> str:
    """Journal field names: uppercase letters, digits, underscore; no leading _."""
    out = "".join(c if c.isalnum() else "_" for c in name.upper())
    return out.lstrip("_") or "FIELD"


def journal_payload(r: Record, identifier: str = IDENTIFIER) -> bytes:
    parts = [
        _field("MESSAGE", f"{r.reason} {r.object}: {r.message}"),
        _field("PRIORITY", r.priority),
        _field("SYSLOG_IDENTIFIER", identifier),
        _field("R7S_TYPE", r.type),
        _field("R7S_REASON", r.reason),
        _field("R7S_OBJECT", r.object),
        _field("R7S_TEXT", r.message),
    ]
    if r.rig:
        parts.append(_field("R7S_RIG", r.rig))
    for k, v in r.fields.items():
        parts.append(_field(f"R7S_{_field_name(k)}", v))
    return b"".join(parts)


class JournalSink:
    name = "journal"

    def __init__(self, path: str = JOURNAL_SOCKET, identifier: str = IDENTIFIER):
        self.path = path
        self.identifier = identifier
        self.dropped = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    @staticmethod
    def available(path: str = JOURNAL_SOCKET) -> bool:
        return os.path.exists(path) and os.access(path, os.W_OK)

    def write(self, r: Record) -> None:
        try:
            self._sock.sendto(journal_payload(r, self.identifier)[:64 * 1024], self.path)
        except OSError as e:
            self.dropped += 1
            if self.dropped in (1, 100, 10000):
                log.warning("journal write failed (%d dropped so far): %s", self.dropped, e)

    def close(self) -> None:
        self._sock.close()


# --- JSON lines file ------------------------------------------------------------


class JsonFileSink:
    """Append-only JSON lines with size-based rotation (events.jsonl, .1, .2 …)."""

    name = "file"

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 (kept open)

    def _rotate(self) -> None:
        self._f.close()
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        if self.backups > 0:
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    def write(self, r: Record) -> None:
        line = r.to_json_line() + "\n"
        try:
            if self._f.tell() + len(line) > self.max_bytes and self._f.tell() > 0:
                self._rotate()
            self._f.write(line)
            self._f.flush()
        except OSError as e:
            log.warning("event file write failed: %s", e)

    def close(self) -> None:
        self._f.close()


def default_event_file(state_dir: Path) -> Path:
    return Path(state_dir) / "events.jsonl"


def choose_sink(backend: str, state_dir: Path, journal_socket: str = JOURNAL_SOCKET) -> Sink:
    """backend: auto | journal | file. auto = journal when its socket exists."""
    if backend == "journal" or (backend == "auto" and JournalSink.available(journal_socket)):
        return JournalSink(journal_socket)
    return JsonFileSink(default_event_file(state_dir))


# --- the log ----------------------------------------------------------------------


class EventLog:
    """Thread-safe: the reconciler, API threads and the CC router all emit."""

    def __init__(self, sinks: list[Sink] | None = None, memory: int = 500,
                 echo: Callable[[Record], None] | None = None,
                 rig: Callable[[], str | None] = lambda: None):
        self.sinks = list(sinks or [])
        self.recent: collections.deque[Record] = collections.deque(maxlen=memory)
        self._lock = threading.Lock()
        self._echo = echo
        self._rig = rig
        self.listeners: list[Callable[[Record], None]] = []

    def emit(self, type_: str, reason: str, obj: str, message: str, *,
             priority: int | None = None, **fields) -> Record:
        r = Record(time.time(), type_, reason, obj, message,
                   priority_for(type_, reason) if priority is None else priority,
                   self._rig(), {k: v for k, v in fields.items() if v is not None})
        with self._lock:
            last = self.recent[-1] if self.recent else None
            if last and (last.type, last.reason, last.object, last.message) == (type_, reason, obj, message):
                last.count += 1
                last.time = r.time
            else:
                self.recent.append(r)
            for s in self.sinks:
                s.write(r)
        if self._echo is not None:
            self._echo(r)
        for fn in self.listeners:
            try:
                fn(r)
            except Exception as e:
                log.debug("event listener failed: %s", e)
        return r

    @property
    def backend(self) -> str:
        return ",".join(s.name for s in self.sinks) or "memory"

    def close(self) -> None:
        with self._lock:
            for s in self.sinks:
                s.close()
