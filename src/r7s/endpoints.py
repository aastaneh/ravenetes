"""Connection endpoint syntax and canonical device port names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .schema import ALSA_ID_RE, HWID_RE

PortKind = Literal["audio", "midi"]
Role = Literal["source", "sink"]

_AUDIO_RE = re.compile(r"^(capture|playback)_(\d+|l|r)$")
_MIDI_RE = re.compile(r"^midi_(capture|playback)(?:_(\d+))?$")
_INLINE_RE = re.compile(r"^(usb|pci|alsa)/([^/]+)/([^/]+)$")


class EndpointError(ValueError):
    pass


@dataclass(frozen=True)
class DevicePort:
    kind: PortKind
    role: Role
    index: int  # 1-based


def parse_device_port(name: str) -> DevicePort | None:
    if m := _AUDIO_RE.match(name):
        role: Role = "source" if m.group(1) == "capture" else "sink"
        n = m.group(2)
        index = {"l": 1, "r": 2}.get(n) or int(n)
        return DevicePort("audio", role, index) if index >= 1 else None
    if m := _MIDI_RE.match(name):
        role = "source" if m.group(1) == "capture" else "sink"
        index = int(m.group(2)) if m.group(2) else 1
        return DevicePort("midi", role, index) if index >= 1 else None
    return None


@dataclass(frozen=True)
class Endpoint:
    """`node:port` where node is a plugin id or device alias, or an inline
    hardware reference `usb/VVVV:PPPP/port`."""

    raw: str
    node: str  # plugin id, device alias, or "usb/1235:8210"
    port: str
    inline_bus: str | None = None
    inline_id: str | None = None

    @property
    def is_inline(self) -> bool:
        return self.inline_bus is not None


def parse_endpoint(raw: str) -> Endpoint:
    s = raw.strip()
    if m := _INLINE_RE.match(s):
        bus, hwid, port = m.group(1), m.group(2), m.group(3)
        if bus == "alsa":
            if not ALSA_ID_RE.match(hwid):
                raise EndpointError(f"{raw!r}: ALSA card id must look like alsa/pisound/capture_1")
        else:
            hwid = hwid.lower()
            if not HWID_RE.match(hwid):
                raise EndpointError(
                    f"{raw!r}: hardware ID must be vendor:product hex, e.g. {bus}/1235:8210/capture_l"
                )
        return Endpoint(raw, f"{bus}/{hwid}", port, bus, hwid)
    if s.startswith(("usb/", "pci/", "alsa/")):
        raise EndpointError(f"{raw!r}: inline hardware reference must look like usb/1235:8210/<port>")
    node, sep, port = s.partition(":")
    if not sep or not node or not port:
        raise EndpointError(f"{raw!r}: expected <plugin-or-device>:<port>")
    if ":" in port:
        raise EndpointError(f"{raw!r}: too many ':' separators")
    return Endpoint(raw, node, port)
