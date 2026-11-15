"""Resolve Device resources and canonical device ports to JACK port names."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from ..endpoints import DevicePort
from .pipewire import Direction, HwDevice, HwGraph, HwNode


def preferred_selector(node: HwNode) -> str:
    """The most stable name to put in a Device's capture:/playback: field."""
    if node.profile and len(node.profile.split(":")) >= 3:
        return node.profile_part  # UCM endpoint name, e.g. "Speaker", "HDMI1"
    return node.nick or node.description


def select_node(endpoints: list[HwNode], selector: str) -> tuple[HwNode | None, str | None]:
    want = selector.casefold()
    exact = [n for n in endpoints if any(s.casefold() == want for s in n.selector_names())]
    if len(exact) == 1:
        return exact[0], None
    if not exact:
        fuzzy = [
            n for n in endpoints
            if any(want in s.casefold() for s in (n.description, n.name, n.profile or ""))
        ]
        if len(fuzzy) == 1:
            return fuzzy[0], None
        exact = fuzzy
    choices = ", ".join(repr(preferred_selector(n)) for n in (exact or endpoints))
    if exact:
        return None, f"{selector!r} matches more than one endpoint; use one of: {choices}"
    return None, f"no endpoint matches {selector!r}; choose one of: {choices}"


@dataclass
class DeviceResolution:
    device: HwDevice | None = None
    nodes: dict[str, HwNode] = field(default_factory=dict)  # direction -> node
    errors: dict[str, str] = field(default_factory=dict)  # direction | "device" -> message

    @property
    def present(self) -> bool:
        return self.device is not None

    def jack_port(
        self, dp: DevicePort, port_name: str, jack_ports: Collection[str] | None = None
    ) -> tuple[str | None, str | None]:
        """(jack port name, None) or (None, reason)."""
        if dp.kind == "midi":
            return None, "MIDI hardware ports aren't resolved yet"
        if "device" in self.errors:
            return None, self.errors["device"]
        direction: Direction = "capture" if dp.role == "source" else "playback"
        if direction in self.errors:
            return None, self.errors[direction]
        node = self.nodes[direction]
        ports = node.ports(direction)
        if dp.index > len(ports):
            return None, (
                f"{node.label} has {len(ports)} {direction} channel(s); "
                f"{port_name!r} doesn't exist"
            )
        hw = ports[dp.index - 1]
        names = [f"{client}:{hw.name}" for client in node.jack_client_candidates()]
        if jack_ports is None:
            return names[0], None
        for n in names:
            if n in jack_ports:
                return n, None
        return None, f"{names[0]!r} is not visible in JACK"


def resolve_device(
    graph: HwGraph,
    bus: str,
    hwid: str,
    serial: str | None = None,
    capture: str | None = None,
    playback: str | None = None,
) -> DeviceResolution:
    res = DeviceResolution()
    matches = graph.find(bus, hwid, serial)
    what = f"{bus} device {hwid}" + (f" (serial {serial})" if serial else "")
    if not matches:
        res.errors["device"] = f"{what} is not connected"
        return res
    if len(matches) > 1:
        serials = ", ".join(repr(d.serial) for d in matches if d.serial) or "none reported"
        res.errors["device"] = f"{len(matches)} devices match {what}; add 'serial:' (seen: {serials})"
        return res
    dev = res.device = matches[0]
    for direction, sel in (("capture", capture), ("playback", playback)):
        endpoints = dev.endpoints(direction)
        if sel is not None:
            node, err = select_node(endpoints, sel) if endpoints else (None, f"{dev.description} has no {direction} endpoints")
        elif len(endpoints) == 1:
            node, err = endpoints[0], None
        elif not endpoints:
            node, err = None, f"{dev.description} has no {direction} endpoints"
        else:
            choices = ", ".join(repr(preferred_selector(n)) for n in endpoints)
            node, err = None, (
                f"{dev.description} has {len(endpoints)} {direction} endpoints; "
                f"set '{direction}:' on the Device to one of: {choices}"
            )
        if node is not None:
            res.nodes[direction] = node
        else:
            res.errors[direction] = err
    return res
