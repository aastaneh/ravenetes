"""Read r7sd's event log back: from the journal (journalctl) or the JSON file."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .eventlog import IDENTIFIER, PRIORITY_NAMES, Record


class LogReadError(Exception):
    pass


_UNITS = {"s": 1, "sec": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400, "days": 86400}


def parse_time(s: str, now: float | None = None) -> float:
    """journalctl-style times: '5 minutes ago', '-1h', 'today', 'yesterday',
    'now', '14:30', '2026-11-15 14:30[:00]', '@1794780000'."""
    now = time.time() if now is None else now
    t = s.strip().lower()
    if t == "now":
        return now
    if t.startswith("@"):
        return float(t[1:])
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if t == "today":
        return midnight.timestamp()
    if t == "yesterday":
        return (midnight - timedelta(days=1)).timestamp()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)\s+ago", t) or re.fullmatch(r"-(\d+(?:\.\d+)?)\s*([a-z]+)", t)
    if m and m.group(2) in _UNITS:
        return now - float(m.group(1)) * _UNITS[m.group(2)]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        return midnight.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                second=int(m.group(3) or 0)).timestamp()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dt%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).timestamp()
        except ValueError:
            pass
    raise ValueError(f"can't parse time {s!r}; try '5 minutes ago', '14:30', or '2026-11-15 14:30'")


@dataclass
class Filters:
    since: float | None = None
    until: float | None = None
    priority: int | None = None  # this level and more severe
    unit: str | None = None  # "plugin", "plugin/amp", "device/scarlett", "r7sd", ...
    type: str | None = None
    rig: str | None = None

    def match(self, r: Record) -> bool:
        if self.since is not None and r.time < self.since:
            return False
        if self.until is not None and r.time > self.until:
            return False
        if self.priority is not None and r.priority > self.priority:
            return False
        if self.type and r.type != self.type:
            return False
        if self.rig and r.rig != self.rig:
            return False
        if self.unit:
            u = self.unit
            if not (r.object == u or r.object.startswith(u + "/") or r.type == u
                    or (u.endswith("/") and r.object.startswith(u))):
                return False
        return True


# --- JSON file ---------------------------------------------------------------------


def _file_chain(path: Path) -> list[Path]:
    """Rotated files oldest first, then the live file."""
    rotated = sorted(path.parent.glob(path.name + ".*"),
                     key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0, reverse=True)
    return [p for p in rotated if p.suffix[1:].isdigit()] + ([path] if path.exists() else [])


def _parse_line(line: str) -> Record | None:
    try:
        return Record.from_dict(json.loads(line))
    except (ValueError, KeyError, TypeError):
        return None


def read_file(path: Path, f: Filters, lines: int | None = None) -> list[Record]:
    out = []
    for p in _file_chain(Path(path)):
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    r = _parse_line(line)
                    if r is not None and f.match(r):
                        out.append(r)
        except OSError:
            continue
    return tail(out, lines)


def tail(items: list, lines: int | None) -> list:
    """Last `lines` items; None = all, 0 = none (unlike items[-0:])."""
    if lines is None:
        return items
    return items[-lines:] if lines > 0 else []


def follow_file(path: Path, f: Filters, poll: float = 0.25) -> Iterator[Record]:
    """Yield records appended after now, surviving rotation."""
    path = Path(path)
    fh = None
    ino = None
    while True:
        if fh is None:
            try:
                fh = open(path, encoding="utf-8")  # noqa: SIM115
                ino = os.fstat(fh.fileno()).st_ino
                fh.seek(0, os.SEEK_END)
            except FileNotFoundError:
                time.sleep(poll)
                continue
        line = fh.readline()
        if line:
            r = _parse_line(line)
            if r is not None and f.match(r):
                yield r
            continue
        try:
            st = os.stat(path)
            if st.st_ino != ino:  # rotated: drain the old file, then reopen from the start
                for rest in fh:
                    r = _parse_line(rest)
                    if r is not None and f.match(r):
                        yield r
                fh.close()
                fh = open(path, encoding="utf-8")  # noqa: SIM115
                ino = os.fstat(fh.fileno()).st_ino
                continue
        except FileNotFoundError:
            pass
        time.sleep(poll)


# --- journal -----------------------------------------------------------------------


def record_from_journal(e: dict) -> Record | None:
    if "R7S_TYPE" not in e:
        return None  # plain stderr line from the service, not an event
    fields = {}
    for k, v in e.items():
        if k.startswith("R7S_") and k not in ("R7S_TYPE", "R7S_REASON", "R7S_OBJECT", "R7S_RIG", "R7S_TEXT"):
            fields[k[4:].lower()] = v
    try:
        ts = int(e.get("_SOURCE_REALTIME_TIMESTAMP") or e["__REALTIME_TIMESTAMP"]) / 1e6
    except (KeyError, ValueError):
        ts = 0.0
    return Record(
        time=ts, type=e.get("R7S_TYPE", ""), reason=e.get("R7S_REASON", ""),
        object=e.get("R7S_OBJECT", ""), message=e.get("R7S_TEXT") or e.get("MESSAGE", ""),
        priority=int(e.get("PRIORITY", 6)), rig=e.get("R7S_RIG"), fields=fields,
    )


def journal_args(f: Filters, raw_since: str | None, raw_until: str | None, lines: int | None,
                 follow: bool, user: bool = True) -> list[str]:
    args = ["journalctl", "-o", "json", "--no-pager", f"SYSLOG_IDENTIFIER={IDENTIFIER}"]
    if user:
        args.insert(1, "--user")
    if f.type:
        args.append(f"R7S_TYPE={f.type}")
    if f.unit and "/" in f.unit and not f.unit.endswith("/"):
        args.append(f"R7S_OBJECT={f.unit}")
    if f.rig:
        args.append(f"R7S_RIG={f.rig}")
    if f.priority is not None:
        args += ["-p", PRIORITY_NAMES[f.priority]]
    if raw_since:
        args += ["--since", raw_since]
    if raw_until:
        args += ["--until", raw_until]
    if lines:
        args += ["-n", str(lines)]
    if follow:
        args.append("-f")
    return args


def read_journal(f: Filters, raw_since=None, raw_until=None, lines=None, follow=False) -> Iterator[Record]:
    exe = shutil.which("journalctl")
    if exe is None:
        raise LogReadError("journalctl not found")
    args = journal_args(f, raw_since, raw_until, lines, follow)
    args[0] = exe
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                r = record_from_journal(json.loads(line))
            except ValueError:
                continue
            if r is not None and f.match(r):
                yield r
        rc = proc.wait()
        if rc not in (0, 1):
            raise LogReadError(f"journalctl failed: {proc.stderr.read().strip() if proc.stderr else rc}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()

