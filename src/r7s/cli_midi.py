"""r7sctl midi: discover MIDI hardware and sniff controls (runs locally)."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field

import click
import yaml

from .hw.alsa import AlsaCard, find_cards, list_cards
from .hw.midi import MidiUnavailable, SeqPort, list_seq_ports
from .scaffold import slug


def _err(msg: str) -> None:
    click.echo(f"r7sctl: {msg}", err=True)


def _table(rows, headers) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(str(c))) for w, c in zip(widths, r)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*headers).rstrip()] + [fmt.format(*map(str, r)).rstrip() for r in rows])


def parse_duration(s: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*", s)
    if not m:
        raise click.BadParameter(f"{s!r}: expected e.g. 30s, 2m, 500ms")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class MidiDevice:
    card: AlsaCard | None
    ports: list[SeqPort] = field(default_factory=list)
    needs_serial: bool = False  # another identical USB unit is plugged in

    @property
    def name(self) -> str:
        if self.card is not None:
            return slug(self.card.id)
        p = self.ports[0]
        return slug(p.client_name) or f"seq-{p.client}"

    @property
    def spec(self) -> dict | None:
        """A Device spec that identifies this hardware, or None (not a card)."""
        c = self.card
        if c is None:
            return None
        if c.bus in ("usb", "pci") and c.hwid:
            spec = {c.bus: c.hwid}
            if self.needs_serial and c.serial:
                spec["serial"] = c.serial
            return spec
        return {"alsa": c.id}

    @property
    def ident(self) -> str:
        s = self.spec
        if s is None:
            return "-"
        k = next(iter(s))
        return f"{k}/{s[k]}"


def _inventory() -> tuple[list[MidiDevice], list[AlsaCard]]:
    try:
        ports = list_seq_ports()
    except MidiUnavailable as e:
        _err(str(e))
        sys.exit(1)
    cards = list_cards()
    by_card = {c.number: c for c in cards}
    devices: dict[object, MidiDevice] = {}
    for p in ports:
        if p.client == 0 or p.client_name.startswith(("PipeWire-", "r7s")) or not (p.source or p.sink):
            continue
        key = ("card", p.card) if p.card is not None else ("client", p.client)
        d = devices.setdefault(key, MidiDevice(by_card.get(p.card)))
        d.ports.append(p)
    for d in devices.values():
        c = d.card
        d.needs_serial = bool(c and c.bus == "usb" and c.hwid and len(find_cards(cards, "usb", c.hwid)) > 1)
    return list(devices.values()), cards


def _resolve_target(target: str, devs: list[MidiDevice]) -> list[SeqPort]:
    """Sources for `midi listen TARGET`: usb/…, pci/…, alsa/…, a card id, or CLIENT:PORT."""
    m = re.fullmatch(r"(\d+):(\d+)", target)
    if m:
        addr = (int(m.group(1)), int(m.group(2)))
        hits = [p for d in devs for p in d.ports if p.address == addr and p.source]
        if not hits:
            _err(f"no readable sequencer port {target}")
            sys.exit(1)
        return hits
    bus, _, ident = target.partition("/")
    for d in devs:
        c = d.card
        if c is None:
            continue
        if (ident and c.matches(bus, ident.lower() if bus != "alsa" else ident)) or (not ident and c.id == target):
            return [p for p in d.ports if p.source]
    names = ", ".join(d.ident if d.spec else f"{d.ports[0].client}:{d.ports[0].port}" for d in devs)
    _err(f"no MIDI device matches {target!r}; try one of: {names or '(none)'}")
    sys.exit(1)


def _listen(sources: list[SeqPort], timeout: float | None, on_event) -> None:
    from alsa_midi import (  # noqa: PLC0415
        ControlChangeEvent,
        NoteOnEvent,
        PitchBendEvent,
        PortCaps,
        PortType,
        ProgramChangeEvent,
        SequencerClient,
    )

    seq = SequencerClient("r7sctl-listen")
    port = seq.create_port("in", caps=PortCaps.WRITE | PortCaps.SUBS_WRITE,
                           type=PortType.MIDI_GENERIC | PortType.APPLICATION)
    names = {}
    for s in sources:
        try:
            seq.subscribe_port(s.address, (seq.client_id, port.port_id))
            names[s.address] = s.name
        except Exception as e:
            _err(f"cannot listen to {s.client}:{s.port} ({s.name}): {e}")
    if not names:
        seq.close()
        sys.exit(1)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while deadline is None or time.monotonic() < deadline:
            wait = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
            ev = seq.event_input(timeout=wait)
            if ev is None or ev.source is None:
                continue
            src = (ev.source.client_id, ev.source.port_id)
            if isinstance(ev, ControlChangeEvent):
                on_event("cc", src, names.get(src, ""), ev.channel + 1, ev.param, ev.value)
            elif isinstance(ev, ProgramChangeEvent):
                on_event("program", src, names.get(src, ""), ev.channel + 1, ev.value, None)
            elif isinstance(ev, NoteOnEvent):
                on_event("note", src, names.get(src, ""), ev.channel + 1, ev.note, ev.velocity)
            elif isinstance(ev, PitchBendEvent):
                on_event("pitchbend", src, names.get(src, ""), ev.channel + 1, None, ev.value)
    except KeyboardInterrupt:
        pass
    finally:
        seq.close()


def register(main: click.Group) -> None:

    @main.group()
    def midi():
        """Discover MIDI hardware and sniff controls."""

    @midi.command("devices")
    @click.option("-o", "--output", type=click.Choice(["table", "yaml", "json"]), default="table",
                  show_default=True)
    def midi_devices(output):
        """List MIDI hardware. -o yaml prints Device resources."""
        devs, cards = _inventory()
        if output == "json":
            click.echo(json.dumps([{
                "name": d.name, "device": d.spec, "card": d.card.number if d.card else None,
                "ports": [{"address": f"{p.client}:{p.port}", "name": p.name,
                           "input": p.source, "output": p.sink} for p in d.ports],
            } for d in devs], indent=2))
            return
        if output == "yaml":
            docs = [{"apiVersion": "r7s/v1", "kind": "Device", "metadata": {"name": d.name}, "spec": d.spec}
                    for d in devs if d.spec]
            for d in devs:
                if not d.spec:
                    click.echo(f"# skipped {d.ports[0].client_name}: not a sound card "
                               "(software port; connect it by hand)", err=True)
            click.echo(yaml.safe_dump_all(docs, sort_keys=False), nl=False)
            return
        if not devs:
            click.echo("No MIDI ports found.")
            return
        rows = []
        for d in devs:
            ins = sum(p.source for p in d.ports)
            outs = sum(p.sink for p in d.ports)
            rows.append([d.name, d.ident, d.card.number if d.card else "-", ins, outs,
                         ", ".join(f"{p.client}:{p.port} {p.name}" for p in d.ports)])
        click.echo(_table(rows, ["NAME", "DEVICE", "CARD", "IN", "OUT", "PORTS"]))

    @midi.command("listen")
    @click.argument("target")
    @click.option("--as", "alias", help="Device alias to print in mappings (default: from the card id).")
    @click.option("--timeout", default=None, help="Stop after e.g. 30s (default: until Ctrl-C).")
    @click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
    def midi_listen(target, alias, timeout, output):
        """Print MIDI from one device as it arrives, CCs as ready-to-paste mappings.

        TARGET is usb/VVVV:PPPP, pci/VVVV:PPPP, alsa/ID, a card id, or CLIENT:PORT.
        """
        devs, _ = _inventory()
        sources = _resolve_target(target, devs)
        if not sources:
            _err(f"{target} has no MIDI inputs")
            sys.exit(1)
        dev = next((d for d in devs if sources[0] in d.ports), None)
        name = alias or (dev.name if dev else "device")
        if output == "text":
            click.echo(f"# listening to {', '.join(f'{s.client}:{s.port} {s.name}' for s in sources)}"
                       " (Ctrl-C to stop)", err=True)

        def show(kind, src, port_name, ch, num, val):
            if output == "json":
                click.echo(json.dumps({"type": kind, "source": f"{src[0]}:{src[1]}", "channel": ch,
                                       "number": num, "value": val}))
            elif kind == "cc":
                click.echo(f"{{ cc: {num}, channel: {ch}, device: {name} }}  # value {val}")
            elif kind == "program":
                click.echo(f"# program change {num} on channel {ch}")
            elif kind == "note":
                click.echo(f"# note {num} velocity {val} on channel {ch}")
            else:
                click.echo(f"# pitch bend {val} on channel {ch}")
            sys.stdout.flush()

        _listen(sources, parse_duration(timeout) if timeout else None, show)

    @midi.command("sniff")
    @click.option("--timeout", default="30s", show_default=True, help="How long to listen.")
    @click.option("-o", "--output", type=click.Choice(["yaml", "json"]), default="yaml", show_default=True)
    def midi_sniff(timeout, output):
        """Listen to every MIDI device, then summarize the controls you touched."""
        devs, _ = _inventory()
        sources = [p for d in devs for p in d.ports if p.source]
        if not sources:
            _err("no MIDI inputs to listen to")
            sys.exit(1)
        owner = {p.address: d for d in devs for p in d.ports}
        seen: dict[tuple[str, int, int], list[int]] = {}
        secs = parse_duration(timeout)
        click.echo(f"# move every knob, fader and button you want to map ({secs:g}s)...", err=True)

        def collect(kind, src, port_name, ch, num, val):
            if kind != "cc":
                return
            d = owner.get(src)
            key = (d.name if d else f"{src[0]}:{src[1]}", ch, num)
            if key not in seen:
                click.echo(f"#   {key[0]}: cc {num} channel {ch}", err=True)
            r = seen.setdefault(key, [val, val])
            r[0], r[1] = min(r[0], val), max(r[1], val)

        _listen(sources, secs, collect)
        controls = [{"device": dev, "channel": ch, "cc": cc, "seen": {"min": lo, "max": hi},
                     "kind": "button" if {lo, hi} <= {0, 127} and lo != hi else "continuous"}
                    for (dev, ch, cc), (lo, hi) in sorted(seen.items())]
        if output == "json":
            click.echo(json.dumps(controls, indent=2))
        elif not controls:
            click.echo("# no CC messages seen")
        else:
            click.echo("# controls seen; paste a mapping onto a parameter, e.g.")
            click.echo("#   DRIVE: { value: 0.5, cc: 16, channel: 1, device: midimix }")
            for c in controls:
                click.echo(f"- {{ cc: {c['cc']}, channel: {c['channel']}, device: {c['device']} }}"
                           f"  # {c['kind']}, seen {c['seen']['min']}..{c['seen']['max']}")
