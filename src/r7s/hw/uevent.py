"""Kernel hotplug events (uevents) over netlink, no libudev needed.

The kernel announces device add/remove on NETLINK_KOBJECT_UEVENT before
udev has applied permissions and before PipeWire has created its nodes, so
callers should re-check hardware a few times shortly after an event.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

NETLINK_KOBJECT_UEVENT = 15
KERNEL_GROUP = 1
SUBSYSTEMS = frozenset({"sound", "snd_seq"})


@dataclass(frozen=True)
class Uevent:
    action: str  # add | remove | change | bind | unbind
    devpath: str
    subsystem: str | None
    props: dict

    @property
    def card(self) -> str | None:
        """'card2' for /devices/.../sound/card2, else None."""
        last = self.devpath.rsplit("/", 1)[-1]
        return last if last.startswith("card") and last[4:].isdigit() else None


def parse_uevent(data: bytes) -> Uevent | None:
    """Kernel format: 'action@devpath\\0KEY=value\\0...'. Ignores udev's
    libudev-formatted messages (they start with 'libudev')."""
    if data.startswith(b"libudev"):
        return None
    parts = data.split(b"\0")
    head = parts[0].decode("utf-8", "replace")
    if "@" not in head:
        return None
    action, devpath = head.split("@", 1)
    props = {}
    for p in parts[1:]:
        if b"=" in p:
            k, v = p.split(b"=", 1)
            props[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return Uevent(props.get("ACTION", action), props.get("DEVPATH", devpath), props.get("SUBSYSTEM"), props)


def is_relevant(ev: Uevent) -> bool:
    """Sound cards coming and going (the card node itself, not every PCM)."""
    return ev.subsystem in SUBSYSTEMS and ev.action in ("add", "remove") and ev.card is not None


class UeventMonitor:
    def __init__(self, on_event: Callable[[Uevent], None]):
        self.on_event = on_event
        self.error: str | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
            s.bind((0, KERNEL_GROUP))
            s.settimeout(0.5)
        except (OSError, AttributeError) as e:  # containers, non-Linux
            self.error = f"hotplug events unavailable: {e}"
            log.info("%s; falling back to periodic hardware checks", self.error)
            return False
        self._sock = s
        self._thread = threading.Thread(target=self._run, name="uevent", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65536)
            except TimeoutError:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    log.warning("uevent socket error: %s", e)
                return
            ev = parse_uevent(data)
            if ev is not None and is_relevant(ev):
                try:
                    self.on_event(ev)
                except Exception:
                    log.exception("uevent handler failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
