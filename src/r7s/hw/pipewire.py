"""Hardware discovery from PipeWire's object graph (`pw-dump`).

PipeWire tags each ALSA card with its bus IDs (device.vendor.id,
device.product.id, device.serial) and groups its PCMs into nodes. Each node
has physical ports that pipewire-jack exposes as `<node.description>:<port>`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["capture", "playback"]


class PipeWireError(Exception):
    pass


@dataclass
class HwPort:
    id: int
    name: str  # port.name, e.g. "capture_FL" or "capture_AUX0"
    index: int  # port.id within the node; defines channel order
    channel: str | None
    alias: str | None


@dataclass
class HwNode:
    id: int
    device_id: int | None
    name: str  # node.name
    description: str
    nick: str | None
    media_class: str
    profile: str | None  # device.profile.name, e.g. "HiFi: Speaker: sink"
    capture_ports: list[HwPort] = field(default_factory=list)  # physical outputs
    playback_ports: list[HwPort] = field(default_factory=list)  # physical inputs

    @property
    def profile_part(self) -> str | None:
        """The endpoint part of a UCM profile name: 'HiFi: Speaker: sink' -> 'Speaker'."""
        if not self.profile:
            return None
        parts = [p.strip() for p in self.profile.split(":")]
        return parts[1] if len(parts) >= 3 else self.profile

    @property
    def label(self) -> str:
        return self.nick or self.profile_part or self.description

    def ports(self, direction: Direction) -> list[HwPort]:
        return self.capture_ports if direction == "capture" else self.playback_ports

    def selector_names(self) -> list[str]:
        out = []
        for v in (self.profile_part, self.nick, self.profile, self.description, self.name):
            if v and v not in out:
                out.append(v)
        return out

    def jack_client_candidates(self) -> list[str]:
        out = []
        for v in (self.description, self.nick, self.name):
            if v and v not in out:
                out.append(v)
        return out


@dataclass
class HwDevice:
    id: int
    bus: str | None  # "usb" | "pci" | ...
    vendor_id: str | None  # "1235"
    product_id: str | None  # "8210"
    serial: str | None
    description: str
    name: str
    sysfs_path: str | None
    nick: str | None = None
    alsa_id: str | None = None  # ALSA card id, e.g. "sofhdadsp"
    card: int | None = None  # ALSA card number
    nodes: list[HwNode] = field(default_factory=list)

    @property
    def hwid(self) -> str | None:
        if self.vendor_id and self.product_id:
            return f"{self.vendor_id}:{self.product_id}"
        return None

    def endpoints(self, direction: Direction) -> list[HwNode]:
        return [n for n in self.nodes if n.ports(direction)]


def _hex4(v) -> str | None:
    if v is None:
        return None
    s = str(v).lower().removeprefix("0x")
    try:
        return f"{int(s, 16):04x}"
    except ValueError:
        return None


@dataclass(frozen=True)
class HwMidiPort:
    """A port on PipeWire's ALSA sequencer bridge."""

    client: int  # ALSA sequencer client
    port: int
    direction: Direction  # capture = MIDI coming from the device
    jack_name: str
    alias: str | None = None


_SEQ_PATH = re.compile(r"^alsa:seq:[^:]*:client_(\d+):(capture|playback)_(\d+)$")


@dataclass
class HwGraph:
    devices: list[HwDevice] = field(default_factory=list)
    midi_ports: list[HwMidiPort] = field(default_factory=list)

    def midi_jack_name(self, client: int, port: int, direction: Direction) -> str | None:
        for m in self.midi_ports:
            if (m.client, m.port, m.direction) == (client, port, direction):
                return m.jack_name
        return None

    @classmethod
    def from_dump(cls, objects: list[dict]) -> HwGraph:
        devices: dict[int, HwDevice] = {}
        nodes: dict[int, HwNode] = {}
        bridges: dict[int, str] = {}  # Midi/Bridge node id -> node.name (its JACK client)
        ports: list[tuple[int, str, dict]] = []
        for o in objects:
            t = o.get("type", "")
            info = o.get("info") or {}
            props = info.get("props") or {}
            if t == "PipeWire:Interface:Device":
                if props.get("device.api") != "alsa":
                    continue
                devices[o["id"]] = HwDevice(
                    id=o["id"],
                    bus=props.get("device.bus"),
                    vendor_id=_hex4(props.get("device.vendor.id")),
                    product_id=_hex4(props.get("device.product.id")),
                    serial=props.get("device.serial"),
                    description=props.get("device.description") or props.get("device.name", ""),
                    name=props.get("device.name", ""),
                    sysfs_path=props.get("device.sysfs.path"),
                    nick=props.get("device.nick"),
                    alsa_id=props.get("alsa.id"),
                    card=_int(props.get("api.alsa.card", props.get("alsa.card"))),
                )
            elif t == "PipeWire:Interface:Node":
                mc = props.get("media.class", "")
                if mc == "Midi/Bridge":
                    bridges[o["id"]] = props.get("node.name", "Midi-Bridge")
                    continue
                if not mc.startswith("Audio/"):
                    continue
                nodes[o["id"]] = HwNode(
                    id=o["id"],
                    device_id=props.get("device.id"),
                    name=props.get("node.name", ""),
                    description=props.get("node.description") or props.get("node.name", ""),
                    nick=props.get("node.nick"),
                    media_class=mc,
                    profile=props.get("device.profile.name"),
                )
            elif t == "PipeWire:Interface:Port":
                ports.append((o["id"], info.get("direction") or props.get("port.direction", ""), props))

        midi: list[HwMidiPort] = []
        for pid, direction, props in ports:
            if props.get("node.id") in bridges:
                m = _SEQ_PATH.match(props.get("object.path", ""))
                if m:
                    midi.append(HwMidiPort(
                        client=int(m.group(1)), port=int(m.group(3)),
                        direction="capture" if m.group(2) == "capture" else "playback",
                        jack_name=f"{bridges[props['node.id']]}:{props.get('port.name', '')}",
                        alias=props.get("port.alias"),
                    ))
                continue
            node = nodes.get(props.get("node.id"))
            if node is None or props.get("port.monitor") or not props.get("port.physical"):
                continue
            port = HwPort(
                id=pid,
                name=props.get("port.name", ""),
                index=int(props.get("port.id", 0)),
                channel=props.get("audio.channel"),
                alias=props.get("port.alias"),
            )
            if direction in ("output", "out"):
                node.capture_ports.append(port)
            elif direction in ("input", "in"):
                node.playback_ports.append(port)
        for n in nodes.values():
            n.capture_ports.sort(key=lambda p: p.index)
            n.playback_ports.sort(key=lambda p: p.index)
            if (dev := devices.get(n.device_id)) is not None:
                dev.nodes.append(n)
        for d in devices.values():
            d.nodes.sort(key=lambda n: (n.media_class, n.label.lower()))
        return cls(sorted(devices.values(), key=lambda d: d.id),
                   sorted(midi, key=lambda m: (m.client, m.port, m.direction)))

    def find(self, bus: str, hwid: str, serial: str | None = None) -> list[HwDevice]:
        out = []
        for d in self.devices:
            if bus == "alsa":
                if d.alsa_id == hwid:
                    out.append(d)
                continue
            if d.bus != bus or d.hwid != hwid:
                continue
            if serial is not None and not serial_matches(d.serial, serial):
                continue
            out.append(d)
        return out


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def serial_matches(device_serial: str | None, wanted: str) -> bool:
    """udev's ID_SERIAL is 'Vendor_Model_SERIAL'; accept the bare serial too."""
    if not device_serial:
        return False
    return device_serial == wanted or device_serial.endswith("_" + wanted)


def pw_dump(timeout: float = 5.0) -> list[dict]:
    exe = shutil.which("pw-dump")
    if exe is None:
        raise PipeWireError("pw-dump not found; hardware resolution needs PipeWire")
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=timeout, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise PipeWireError(f"pw-dump failed: {e.stderr.strip() or e}") from e
    except subprocess.TimeoutExpired as e:
        raise PipeWireError("pw-dump timed out; is PipeWire running?") from e
    try:
        return json.loads(out)
    except ValueError as e:
        raise PipeWireError(f"pw-dump returned invalid JSON: {e}") from e


def snapshot() -> HwGraph:
    return HwGraph.from_dump(pw_dump())
