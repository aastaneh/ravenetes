"""ALSA card inventory from sysfs: card number, card id, and bus IDs.

PipeWire only creates Device objects for cards with PCMs, so a MIDI-only
controller has none. sysfs knows every card: /sys/class/sound/cardN has
`number` and `id`, and its `device` link leads up to the USB device
(idVendor/idProduct/serial) or PCI function (vendor/device).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AlsaCard:
    number: int
    id: str  # e.g. "pisound", "MIDImix", "sofhdadsp"
    bus: str | None  # "usb" | "pci" | None (platform/virtual)
    vendor_id: str | None = None
    product_id: str | None = None
    serial: str | None = None

    @property
    def hwid(self) -> str | None:
        if self.vendor_id and self.product_id:
            return f"{self.vendor_id}:{self.product_id}"
        return None

    def matches(self, bus: str, hwid: str, serial: str | None = None) -> bool:
        if bus == "alsa":
            return self.id == hwid
        if self.bus != bus or self.hwid != hwid:
            return False
        if serial is None:
            return True
        return self.serial is not None and (self.serial == serial or self.serial.endswith("_" + serial))


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _hex4(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return f"{int(v.removeprefix('0x'), 16):04x}"
    except ValueError:
        return None


def _bus_ids(device: Path, sysfs: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """Walk up from a card's device to the USB device or PCI function."""
    d = device
    stop = sysfs.resolve()
    while d != d.parent and d != stop:
        if (d / "idVendor").is_file() and (d / "idProduct").is_file():
            return "usb", _hex4(_read(d / "idVendor")), _hex4(_read(d / "idProduct")), _read(d / "serial")
        if (d / "vendor").is_file() and (d / "device").is_file() and (d / "class").is_file():
            return "pci", _hex4(_read(d / "vendor")), _hex4(_read(d / "device")), None
        d = d.parent
    return None, None, None, None


def list_cards(sysfs: str | os.PathLike = "/sys") -> list[AlsaCard]:
    root = Path(sysfs)
    cards = []
    for c in sorted((root / "class" / "sound").glob("card*")):
        number = _read(c / "number")
        card_id = _read(c / "id")
        if number is None or card_id is None or not number.isdigit():
            continue
        dev = c / "device"
        bus = vid = pid = serial = None
        if dev.exists():
            bus, vid, pid, serial = _bus_ids(dev.resolve(), root)
        cards.append(AlsaCard(int(number), card_id, bus, vid, pid, serial))
    return sorted(cards, key=lambda c: c.number)


def find_cards(cards: list[AlsaCard], bus: str, hwid: str, serial: str | None = None) -> list[AlsaCard]:
    return [c for c in cards if c.matches(bus, hwid, serial)]
