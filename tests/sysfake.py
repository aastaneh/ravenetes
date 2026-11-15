"""Build a minimal fake /sys with ALSA cards attached to USB, PCI or platform devices."""

from __future__ import annotations

import os
from pathlib import Path


def card(root: Path, number: int, card_id: str, bus: str | None, vid: str = "", pid: str = "",
         serial: str | None = None) -> None:
    devices = root / "devices"
    if bus == "usb":
        usbdev = devices / "pci0000:00" / "0000:00:14.0" / "usb1" / f"1-{number}"
        usbdev.mkdir(parents=True, exist_ok=True)
        (usbdev / "idVendor").write_text(vid + "\n")
        (usbdev / "idProduct").write_text(pid + "\n")
        if serial:
            (usbdev / "serial").write_text(serial + "\n")
        parent = usbdev / f"1-{number}:1.0"
    elif bus == "pci":
        fn = devices / "pci0000:00" / f"0000:00:1f.{number}"
        fn.mkdir(parents=True, exist_ok=True)
        (fn / "vendor").write_text(f"0x{vid}\n")
        (fn / "device").write_text(f"0x{pid}\n")
        (fn / "class").write_text("0x040300\n")
        parent = fn
    else:
        parent = devices / "platform" / f"soc:sound{number}"
    carddir = parent / "sound" / f"card{number}"
    carddir.mkdir(parents=True, exist_ok=True)
    (carddir / "number").write_text(f"{number}\n")
    (carddir / "id").write_text(card_id + "\n")
    os.symlink(parent, carddir / "device")
    cls = root / "class" / "sound"
    cls.mkdir(parents=True, exist_ok=True)
    os.symlink(carddir, cls / f"card{number}")
