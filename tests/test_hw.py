import json

import pytest

from r7s.endpoints import parse_device_port
from r7s.hw.pipewire import HwGraph
from r7s.hw.resolve import resolve_device

from . import pwfake
from .conftest import FIXTURES

LAPTOP = json.loads((FIXTURES / "pw" / "laptop-hda.json").read_text())


def port(res, name, jack=None):
    return res.jack_port(parse_device_port(name), name, jack)


def test_thinkpad_graph_shape():
    g = HwGraph.from_dump(LAPTOP)
    (dev,) = g.devices
    assert (dev.bus, dev.hwid) == ("pci", "8086:51c8")
    assert sorted(preferred for preferred in (n.profile_part for n in dev.endpoints("playback"))) == [
        "HDMI1", "HDMI2", "HDMI3", "Speaker"]
    assert sorted(n.profile_part for n in dev.endpoints("capture")) == ["Mic1", "Mic2"]
    speaker = next(n for n in dev.nodes if n.profile_part == "Speaker")
    # Monitor ports are excluded from capture.
    assert [p.name for p in speaker.playback_ports] == ["playback_FL", "playback_FR"]
    assert speaker.capture_ports == []


def test_ambiguous_endpoints_need_selector():
    g = HwGraph.from_dump(LAPTOP)
    res = resolve_device(g, "pci", "8086:51c8")
    assert res.present
    assert "4 playback endpoints; set 'playback:'" in res.errors["playback"]
    assert "'Speaker'" in res.errors["playback"] and "'HDMI1'" in res.errors["playback"]
    assert port(res, "playback_1") == (None, res.errors["playback"])


def test_selectors_pick_endpoints():
    g = HwGraph.from_dump(LAPTOP)
    res = resolve_device(g, "pci", "8086:51c8", capture="Stereo Microphone", playback="speaker")
    assert res.errors == {}
    assert port(res, "playback_l") == (
        "Alder Lake PCH-P High Definition Audio Controller Speaker:playback_FL", None)
    assert port(res, "capture_2")[0].endswith("Stereo Microphone:capture_FR")
    assert "has 2 playback channel(s); 'playback_3' doesn't exist" in port(res, "playback_3")[1]
    res = resolve_device(g, "pci", "8086:51c8", playback="HDMI")
    assert "matches more than one endpoint" in res.errors["playback"]
    res = resolve_device(g, "pci", "8086:51c8", playback="Headphones")
    assert "no endpoint matches 'Headphones'" in res.errors["playback"]


def test_usb_single_endpoint_needs_no_selector():
    g = HwGraph.from_dump(pwfake.scarlett())
    res = resolve_device(g, "usb", "1235:8210")
    assert res.errors == {}
    assert port(res, "capture_l") == ("Scarlett 2i2 USB Analog Stereo:capture_FL", None)
    assert port(res, "playback_r") == ("Scarlett 2i2 USB Analog Stereo:playback_FR", None)
    assert "no ALSA card number" in port(res, "midi_capture")[1]


def test_identical_units_need_serial():
    g = HwGraph.from_dump(pwfake.scarlett("AAA111") + pwfake.scarlett("BBB222"))
    res = resolve_device(g, "usb", "1235:8210")
    assert "2 devices match" in res.errors["device"] and "add 'serial:'" in res.errors["device"]
    res = resolve_device(g, "usb", "1235:8210", serial="BBB222")
    assert res.device.serial.endswith("BBB222") and res.errors == {}
    assert resolve_device(g, "usb", "1235:8210", serial="ZZZ").errors["device"].endswith("is not connected")


def test_pro_audio_duplex_node():
    g = HwGraph.from_dump(pwfake.umc404_pro_audio())
    res = resolve_device(g, "usb", "1397:0509")
    assert res.errors == {}
    assert port(res, "capture_4") == ("UMC404HD 192k Pro:capture_AUX3", None)
    assert port(res, "playback_1") == ("UMC404HD 192k Pro:playback_AUX0", None)


def test_jack_name_fallback_to_node_name():
    g = HwGraph.from_dump(pwfake.scarlett())
    res = resolve_device(g, "usb", "1235:8210")
    node_name = res.nodes["capture"].name
    assert port(res, "capture_1", {f"{node_name}:capture_FL"}) == (f"{node_name}:capture_FL", None)
    assert "not visible in JACK" in port(res, "capture_1", set())[1]


@pytest.mark.parametrize("hwid", ["dead:beef"])
def test_absent_device(hwid):
    res = resolve_device(HwGraph.from_dump(LAPTOP), "usb", hwid)
    assert not res.present and port(res, "capture_1")[1] == "usb device dead:beef is not connected"
