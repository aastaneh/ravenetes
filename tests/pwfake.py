"""Build pw-dump style object lists for typical USB audio interfaces.

Shapes follow real PipeWire output: an ALSA Device object with udev IDs,
Audio/Source|Sink|Duplex nodes pointing at it, and physical ports.
"""

from __future__ import annotations

import itertools

_ids = itertools.count(1000)


def _device(bus, vid, pid, serial, desc, name):
    return {
        "id": next(_ids),
        "type": "PipeWire:Interface:Device",
        "info": {"props": {
            "device.api": "alsa", "device.bus": bus,
            "device.vendor.id": f"0x{vid}", "device.product.id": f"0x{pid}",
            "device.serial": serial, "device.description": desc, "device.name": name,
        }},
    }


def _node(dev, media_class, name, desc, nick, profile):
    return {
        "id": next(_ids),
        "type": "PipeWire:Interface:Node",
        "info": {"props": {
            "device.id": dev["id"], "media.class": media_class, "node.name": name,
            "node.description": desc, "node.nick": nick, "device.profile.name": profile,
        }},
    }


def _ports(node, direction, names, monitor=False):
    out = []
    for i, n in enumerate(names):
        props = {"node.id": node["id"], "port.name": n, "port.id": i,
                 "port.physical": not monitor, "port.direction": "out" if direction == "output" else "in"}
        if monitor:
            props["port.monitor"] = True
        out.append({"id": next(_ids), "type": "PipeWire:Interface:Port",
                    "info": {"direction": direction, "props": props}})
    return out


def scarlett(serial="Y8ABCDEF"):
    """Class-compliant 2-in/2-out, 'analog-stereo' profile: one source, one sink."""
    udev_serial = f"Focusrite_Scarlett_2i2_USB_{serial}"
    dev = _device("usb", "1235", "8210", udev_serial, "Scarlett 2i2 USB", f"alsa_card.usb-{udev_serial}-00")
    src = _node(dev, "Audio/Source", f"alsa_input.usb-{udev_serial}-00.analog-stereo",
                "Scarlett 2i2 USB Analog Stereo", "Scarlett 2i2 USB", "analog-stereo")
    sink = _node(dev, "Audio/Sink", f"alsa_output.usb-{udev_serial}-00.analog-stereo",
                 "Scarlett 2i2 USB Analog Stereo", "Scarlett 2i2 USB", "analog-stereo")
    return [dev, src, sink,
            *_ports(src, "output", ["capture_FL", "capture_FR"]),
            *_ports(sink, "input", ["playback_FL", "playback_FR"]),
            *_ports(sink, "output", ["monitor_FL", "monitor_FR"], monitor=True)]


def umc404_pro_audio():
    """'pro-audio' profile: a single Duplex node with AUX channels both ways."""
    dev = _device("usb", "1397", "0509", "BEHRINGER_UMC404HD_192k", "UMC404HD 192k",
                  "alsa_card.usb-BEHRINGER_UMC404HD_192k-00")
    duplex = _node(dev, "Audio/Duplex", "alsa_card.usb-BEHRINGER_UMC404HD_192k-00.pro-audio-0",
                   "UMC404HD 192k Pro", "UMC404HD 192k", "pro-audio")
    return [dev, duplex,
            *_ports(duplex, "output", [f"capture_AUX{i}" for i in range(4)]),
            *_ports(duplex, "input", [f"playback_AUX{i}" for i in range(4)])]
