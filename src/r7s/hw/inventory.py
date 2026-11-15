"""One consistent hardware view: PipeWire graph + ALSA cards + sequencer ports."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import pipewire
from .alsa import AlsaCard, list_cards
from .midi import SeqPort, inventory
from .pipewire import HwGraph
from .resolve import DeviceResolution, resolve_device


@dataclass
class Hardware:
    graph: HwGraph
    cards: list[AlsaCard] | None = None
    seq: list[SeqPort] | None = field(default=None)

    def resolve(self, bus: str, hwid: str, serial: str | None = None,
                capture: str | None = None, playback: str | None = None) -> DeviceResolution:
        return resolve_device(self.graph, bus, hwid, serial, capture, playback, self.cards, self.seq)

    def resolve_spec(self, spec) -> DeviceResolution:
        return self.resolve(spec.bus, spec.hwid, spec.serial, spec.capture, spec.playback)


def snapshot(sysfs: str = "/sys") -> Hardware:
    """Raises pipewire.PipeWireError if PipeWire can't be queried."""
    graph = pipewire.snapshot()
    try:
        cards = list_cards(sysfs)
    except OSError:
        cards = None
    return Hardware(graph, cards, inventory(graph))
