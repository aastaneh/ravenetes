"""ALSA sequencer MIDI port inventory.

Card-based sequencer clients carry their ALSA card number, which is how a
Device (usb/pci/alsa) is tied to its MIDI ports. The primary source is the
sequencer itself (via alsa-midi). Without it, ports are derived from
PipeWire's bridge using the kernel's numbering, client = 16 + 4*card + n
(sound/core/seq/seq_clientmgr.c).
"""

from __future__ import annotations

from dataclasses import dataclass

from .pipewire import HwGraph

SEQ_GLOBAL_CLIENTS = 16
SEQ_CLIENTS_PER_CARD = 4
SEQ_DYNAMIC_CLIENTS_BEGIN = 128


class MidiUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SeqPort:
    client: int
    port: int
    name: str
    client_name: str
    card: int | None
    source: bool  # readable: MIDI comes out of it (device "capture")
    sink: bool  # writable: MIDI goes into it (device "playback")

    @property
    def address(self) -> tuple[int, int]:
        return (self.client, self.port)


def card_of_client(client: int) -> int | None:
    if SEQ_GLOBAL_CLIENTS <= client < SEQ_DYNAMIC_CLIENTS_BEGIN:
        return (client - SEQ_GLOBAL_CLIENTS) // SEQ_CLIENTS_PER_CARD
    return None


def list_seq_ports() -> list[SeqPort]:
    """All sequencer ports with their card, straight from ALSA."""
    try:
        from alsa_midi import PortCaps, SequencerClient  # noqa: PLC0415
    except (ImportError, OSError) as e:
        raise MidiUnavailable(f"ALSA sequencer not available: {e}") from e
    try:
        seq = SequencerClient("r7s-inventory")
    except Exception as e:  # alsa_midi raises ALSAError subclasses
        raise MidiUnavailable(f"cannot open the ALSA sequencer: {e}") from e
    out = []
    try:
        ci = seq.query_next_client(-1)
        while ci is not None:
            if ci.client_id != seq.client_id:
                pi = seq.query_next_port(ci.client_id)
                while pi is not None:
                    caps = pi.capability
                    out.append(SeqPort(
                        client=ci.client_id, port=pi.port_id, name=pi.name or "",
                        client_name=ci.name or "", card=ci.card_id,
                        source=bool(caps & PortCaps.READ and caps & PortCaps.SUBS_READ),
                        sink=bool(caps & PortCaps.WRITE and caps & PortCaps.SUBS_WRITE),
                    ))
                    pi = seq.query_next_port(ci.client_id, pi)
            ci = seq.query_next_client(ci)
    finally:
        seq.close()
    return out


def seq_ports_from_pipewire(hw: HwGraph) -> list[SeqPort]:
    """Fallback inventory from PipeWire's bridge ports and kernel numbering."""
    by_addr: dict[tuple[int, int], dict] = {}
    for m in hw.midi_ports:
        d = by_addr.setdefault((m.client, m.port), {"source": False, "sink": False, "alias": m.alias})
        d["source" if m.direction == "capture" else "sink"] = True
    out = []
    for (client, port), d in sorted(by_addr.items()):
        alias = d["alias"] or ""
        client_name, _, name = alias.partition(":")
        out.append(SeqPort(client, port, name or alias, client_name, card_of_client(client),
                           d["source"], d["sink"]))
    return out


def inventory(hw: HwGraph | None) -> list[SeqPort]:
    try:
        return list_seq_ports()
    except MidiUnavailable:
        return seq_ports_from_pipewire(hw) if hw is not None else []


def card_ports(ports: list[SeqPort], cards: set[int]) -> tuple[list[SeqPort], list[SeqPort]]:
    """(capture ports, playback ports) of the given cards, in a stable order."""
    mine = sorted((p for p in ports if p.card in cards), key=lambda p: p.address)
    return [p for p in mine if p.source], [p for p in mine if p.sink]
