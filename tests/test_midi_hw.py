from r7s.endpoints import parse_device_port
from r7s.hw.alsa import list_cards
from r7s.hw.inventory import Hardware
from r7s.hw.midi import SeqPort, card_of_client, seq_ports_from_pipewire
from r7s.hw.pipewire import HwGraph

from . import pwfake, sysfake


def port(res, name):
    return res.jack_port(parse_device_port(name), name)


def seq(client, port, name, card, source=True, sink=True, client_name="X"):
    return SeqPort(client, port, name, client_name, card, source, sink)


def build(tmp_path):
    """Laptop HDA on card 0, a MIDImix (MIDI-only USB) on card 1, a Pisound on card 2."""
    sysfake.card(tmp_path, 0, "sofhdadsp", "pci", "8086", "51c8")
    sysfake.card(tmp_path, 1, "MIDImix", "usb", "09e8", "0031", serial=None)
    sysfake.card(tmp_path, 2, "pisound", None)
    dump = (pwfake.pisound(card=2)
            + pwfake.midi_bridge([(20, 0, "MIDI Mix:MIDI Mix MIDI 1", "both"),
                                  (24, 0, "pisound:pisound MIDI PS-XXXX", "both")]))
    seqs = [seq(14, 0, "Midi Through Port-0", None),
            seq(20, 0, "MIDI Mix MIDI 1", 1),
            seq(24, 0, "pisound MIDI PS-XXXX", 2)]
    return Hardware(HwGraph.from_dump(dump), list_cards(tmp_path), seqs)


def test_sysfs_cards(tmp_path):
    hw = build(tmp_path)
    assert [(c.number, c.id, c.bus, c.hwid) for c in hw.cards] == [
        (0, "sofhdadsp", "pci", "8086:51c8"), (1, "MIDImix", "usb", "09e8:0031"), (2, "pisound", None, None)]


def test_midi_only_usb_device(tmp_path):
    res = build(tmp_path).resolve("usb", "09e8:0031")
    assert res.present and res.device is None and res.card == 1 and res.label == "MIDImix"
    assert port(res, "midi_capture") == ("Midi-Bridge:MIDI Mix MIDI 1 (capture)", None)
    assert port(res, "midi_playback_1") == ("Midi-Bridge:MIDI Mix MIDI 1 (playback)", None)
    assert port(res, "midi_capture_2")[1] == "MIDImix has 1 MIDI inputs; 'midi_capture_2' doesn't exist"
    assert port(res, "capture_1")[1] == "MIDImix has no capture audio endpoints"
    assert res.seq_sources() == [(20, 0)]


def test_alsa_id_device_has_audio_and_midi(tmp_path):
    res = build(tmp_path).resolve("alsa", "pisound")
    assert res.device is not None and res.card == 2 and res.errors == {}
    assert port(res, "capture_l") == ("pisound Stereo:capture_FL", None)
    assert port(res, "midi_capture") == ("Midi-Bridge:pisound MIDI PS-XXXX (capture)", None)


def test_unbridged_midi_port(tmp_path):
    hw = build(tmp_path)
    hw.graph.midi_ports = []  # PipeWire without the seq bridge
    res = hw.resolve("usb", "09e8:0031")
    assert "not bridged into JACK" in port(res, "midi_capture")[1]
    assert res.seq_sources() == [(20, 0)]  # CC mapping still works without JACK


def test_identical_midi_controllers_need_serial(tmp_path):
    sysfake.card(tmp_path, 1, "MIDImix", "usb", "09e8", "0031", serial="AAA")
    sysfake.card(tmp_path, 3, "MIDImix_1", "usb", "09e8", "0031", serial="BBB")
    hw = Hardware(HwGraph(), list_cards(tmp_path), [seq(20, 0, "a", 1), seq(28, 0, "b", 3)])
    assert "2 devices match" in hw.resolve("usb", "09e8:0031").errors["device"]
    res = hw.resolve("usb", "09e8:0031", serial="BBB")
    assert res.card == 3 and res.seq_sources() == [(28, 0)]


def test_audio_device_and_sysfs_card_are_one_device(tmp_path):
    # A Scarlett with MIDI: PipeWire Device (card 1) and sysfs card 1 are the same thing.
    sysfake.card(tmp_path, 1, "USB", "usb", "1235", "8210", serial="Focusrite_Scarlett_2i2_USB_Y8ABCDEF")
    hw = Hardware(HwGraph.from_dump(pwfake.scarlett(card=1)), list_cards(tmp_path), [seq(20, 0, "m", 1)])
    res = hw.resolve("usb", "1235:8210", serial="Y8ABCDEF")
    assert res.errors == {} and res.card == 1 and res.device is not None
    assert res.seq_sources() == [(20, 0)]


def test_fallback_inventory_from_pipewire():
    g = HwGraph.from_dump(pwfake.midi_bridge([(14, 0, "Midi Through:Midi Through Port-0", "both"),
                                             (20, 0, "MIDI Mix:MIDI Mix MIDI 1", "capture")]))
    ports = seq_ports_from_pipewire(g)
    assert [(p.client, p.card, p.source, p.sink) for p in ports] == [(14, None, True, True), (20, 1, True, False)]
    assert (card_of_client(16), card_of_client(23), card_of_client(127), card_of_client(128)) == (0, 1, 27, None)
