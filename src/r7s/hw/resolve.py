"""Resolve Device resources and canonical device ports to JACK port names."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from ..endpoints import DevicePort
from .alsa import AlsaCard, find_cards
from .midi import SeqPort, card_ports
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
class MidiEndpoint:
    seq: SeqPort
    jack_name: str | None  # None if PipeWire's bridge doesn't show it


@dataclass
class DeviceResolution:
    device: HwDevice | None = None  # PipeWire Device (audio); None for MIDI-only cards
    card: int | None = None  # ALSA card number, when known
    label: str = ""
    nodes: dict[str, HwNode] = field(default_factory=dict)  # direction -> node
    errors: dict[str, str] = field(default_factory=dict)  # "device" | direction -> message
    midi: dict[str, list[MidiEndpoint]] | None = None  # direction -> ports
    midi_error: str | None = None

    @property
    def present(self) -> bool:
        return self.device is not None or self.card is not None

    def seq_sources(self) -> list[tuple[int, int]]:
        """Sequencer addresses MIDI comes out of (for CC mapping)."""
        return [m.seq.address for m in (self.midi or {}).get("capture", [])]

    def jack_port(
        self, dp: DevicePort, port_name: str, jack_ports: Collection[str] | None = None
    ) -> tuple[str | None, str | None]:
        """(jack port name, None) or (None, reason)."""
        if "device" in self.errors:
            return None, self.errors["device"]
        direction: Direction = "capture" if dp.role == "source" else "playback"
        if dp.kind == "midi":
            return self._midi_port(direction, dp.index, port_name, jack_ports)
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

    def _midi_port(self, direction, index, port_name, jack_ports):
        if self.midi is None:
            return None, self.midi_error or f"MIDI ports of {self.label} are unknown"
        ports = self.midi.get(direction, [])
        word = "inputs" if direction == "capture" else "outputs"
        if not ports:
            return None, f"{self.label} has no MIDI {word}"
        if index > len(ports):
            return None, f"{self.label} has {len(ports)} MIDI {word}; {port_name!r} doesn't exist"
        m = ports[index - 1]
        if m.jack_name is None:
            return None, (f"MIDI port {m.seq.client}:{m.seq.port} ({m.seq.name}) is not bridged into "
                          "JACK; is PipeWire's ALSA sequencer bridge enabled?")
        if jack_ports is not None and m.jack_name not in jack_ports:
            return None, f"{m.jack_name!r} is not visible in JACK"
        return m.jack_name, None


def resolve_device(
    graph: HwGraph,
    bus: str,
    hwid: str,
    serial: str | None = None,
    capture: str | None = None,
    playback: str | None = None,
    cards: list[AlsaCard] | None = None,
    seq: list[SeqPort] | None = None,
) -> DeviceResolution:
    """Find one physical device across PipeWire (audio) and sysfs (any card),
    joined on the ALSA card number, then pick its endpoints and MIDI ports."""
    res = DeviceResolution()
    what = f"{bus} device {hwid}" + (f" (serial {serial})" if serial else "")
    pw = graph.find(bus, hwid, serial)
    sys_cards = find_cards(cards, bus, hwid, serial) if cards is not None else []
    numbers = {d.card for d in pw if d.card is not None} | {c.number for c in sys_cards}
    unnumbered = [d for d in pw if d.card is None]
    count = len(numbers) + len(unnumbered)
    if count == 0:
        res.errors["device"] = f"{what} is not connected"
        return res
    if count > 1:
        seen = sorted({d.serial for d in pw if d.serial} | {c.serial for c in sys_cards if c.serial})
        serials = ", ".join(map(repr, seen)) or "none reported"
        res.errors["device"] = f"{count} devices match {what}; add 'serial:' (seen: {serials})"
        return res
    if numbers:
        res.card = next(iter(numbers))
        res.device = next((d for d in pw if d.card == res.card), None)
    else:
        res.device = unnumbered[0]
    card = next((c for c in sys_cards if c.number == res.card), None)
    res.label = res.device.description if res.device else (card.id if card else what)

    for direction, sel in (("capture", capture), ("playback", playback)):
        endpoints = res.device.endpoints(direction) if res.device else []
        if not endpoints:
            node, err = None, f"{res.label} has no {direction} audio endpoints"
        elif sel is not None:
            node, err = select_node(endpoints, sel)
        elif len(endpoints) == 1:
            node, err = endpoints[0], None
        else:
            choices = ", ".join(repr(preferred_selector(n)) for n in endpoints)
            node, err = None, (
                f"{res.label} has {len(endpoints)} {direction} endpoints; "
                f"set '{direction}:' on the Device to one of: {choices}"
            )
        if node is not None:
            res.nodes[direction] = node
        else:
            res.errors[direction] = err

    if res.card is None:
        res.midi_error = f"can't tell which MIDI ports belong to {res.label} (no ALSA card number)"
    elif seq is None:
        res.midi_error = "MIDI port inventory unavailable"
    else:
        caps, plays = card_ports(seq, {res.card})
        res.midi = {
            "capture": [MidiEndpoint(p, graph.midi_jack_name(p.client, p.port, "capture")) for p in caps],
            "playback": [MidiEndpoint(p, graph.midi_jack_name(p.client, p.port, "playback")) for p in plays],
        }
    return res
