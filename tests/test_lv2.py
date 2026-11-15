from r7s.lv2.model import Plugin
from r7s.lv2.ttl import load_bundle, parse_bundle
from r7s.lv2.world import PluginWorld, find_bundles

from .conftest import LV2_DIR

BUNDLE = LV2_DIR / "r7s-test.lv2"


def test_parse_bundle_ports_and_metadata():
    plugins = {p.uri: p for p in parse_bundle(BUNDLE)}
    assert set(plugins) == {"urn:r7s:test:amp", "urn:r7s:test:synth"}

    amp = plugins["urn:r7s:test:amp"]
    assert amp.name == "R7S Test Amp"
    assert amp.classes == ["Amplifier"]
    assert amp.ui_types == ["X11UI"]
    assert amp.binary.endswith("amp.so")
    assert [p.symbol for p in amp.ports] == ["in", "out", "gain", "bypass", "mode", "level"]

    gain = amp.port("gain")
    assert (gain.type, gain.direction) == ("control", "input")
    assert (gain.default, gain.minimum, gain.maximum, gain.unit) == (0.0, -24.0, 24.0, "dB")
    assert amp.port("bypass").toggled
    mode = amp.port("mode")
    assert mode.enumeration and mode.integer
    assert [s.label for s in mode.scale_points] == ["Clean", "Crunch", "Lead"]
    assert amp.port("level").direction == "output"


def test_midi_vs_plain_atom_ports():
    synth = next(p for p in parse_bundle(BUNDLE) if p.uri == "urn:r7s:test:synth")
    assert synth.port("midi_in").type == "midi"
    assert synth.port("control").type == "atom"
    assert synth.classes == ["Instrument"]
    assert not synth.has_ui


def test_broken_preset_and_missing_ui_files_are_skipped():
    # The manifest's preset and UI seeAlso files are broken/missing; only
    # plugin seeAlso files are loaded, so parsing still succeeds.
    assert len(parse_bundle(BUNDLE)) == 2


def test_cache_roundtrip(tmp_path):
    misses = []
    first = load_bundle(BUNDLE, tmp_path, on_miss=misses.append)
    second = load_bundle(BUNDLE, tmp_path, on_miss=misses.append)
    assert len(misses) == 1
    assert [p.to_dict() for p in first] == [p.to_dict() for p in second]
    assert Plugin.from_dict(first[0].to_dict()) == first[0]


def test_find_bundles_accepts_bundle_or_parent_dir():
    assert find_bundles([LV2_DIR]) == [BUNDLE.resolve()]
    assert find_bundles([BUNDLE, LV2_DIR, "/nonexistent"]) == [BUNDLE.resolve()]


def test_world_search_and_category(world: PluginWorld):
    assert [p.uri for p in world.search("amp")] == ["urn:r7s:test:amp"]
    assert [p.uri for p in world.search("synth")] == ["urn:r7s:test:synth"]
    assert [p.uri for p in world.search("instrument")] == ["urn:r7s:test:synth"]
    assert [p.uri for p in world.all("amplifier")] == ["urn:r7s:test:amp"]
    assert world.search("sinth")  # fuzzy
    assert world.suggest("urn:r7s:test:ampp")[0] == "urn:r7s:test:amp"
