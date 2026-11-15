import pytest

from r7s.validate import Known, validate_environment, validate_structure

from .conftest import DEVICES, LV2_DIR, parse, rig

AMP = "urn:r7s:test:amp"
SYNTH = "urn:r7s:test:synth"


def t1(text, **kw):
    r = parse(text)
    assert r.report.ok, r.report.diagnostics
    return validate_structure(r, **kw)


def t2(text, world):
    r = parse(text)
    assert r.report.ok, r.report.diagnostics
    assert not [d for d in validate_structure(r) if d.severity == "error"]
    return [d for d in validate_environment(r, world) if d.severity != "info"]


def msgs(ds, severity="error"):
    return [d.message for d in ds if d.severity == severity]


GOOD = DEVICES + "---\n" + rig(
    plugins=f"""
    - id: amp
      lv2: {AMP}
      parameters:
        gain: {{value: 3, cc: 16, channel: 1, device: midimix, min: -6, max: 6}}
        bypass: false
        mode: lead
    - id: synth
      lv2: {SYNTH}
    """,
    audio="""
    - {from: scarlett:capture_l, to: amp:in}
    - {from: amp:out, to: scarlett:playback_1}
    - {from: synth:out_l, to: scarlett:playback_1}
    - {from: synth:out_r, to: usb/1235:8210/playback_r}
    """,
    midi="""
    - {from: midimix:midi_capture, to: synth:midi_in}
    """,
) + "---\napiVersion: r7s/v1\nkind: ActiveRig\nmetadata: {name: current}\nspec: {rig: r}\n"


def test_good_rig_passes_both_tiers(world):
    assert t1(GOOD) == []
    assert t2(GOOD, world) == []


def test_unresolved_refs_warn_offline_and_error_when_strict():
    text = rig(plugins=f"- {{id: amp, lv2: {AMP}}}", audio="- {from: scarlet:capture_1, to: amp:in}")
    assert msgs(t1(text), "warning") == [
        "'scarlet' is not a plugin in this rig or a known Device"
    ]
    assert msgs(t1(text, strict_refs=True)) == ["'scarlet' is not a plugin in this rig or a known Device"]
    assert t1(text, known=Known(devices=frozenset({"scarlet"}))) == []


def test_device_port_rules():
    text = DEVICES + "---\n" + rig(
        plugins=f"- {{id: amp, lv2: {AMP}}}",
        audio="""
        - {from: scarlett:playback_1, to: amp:in}
        - {from: scarlett:capture_0, to: amp:in}
        - {from: scarlett:midi_capture, to: amp:in}
        - {from: scarlett:left, to: amp:in}
        - {from: amp:out, to: usb/1235:821/playback_1}
        """,
    )
    m = msgs(t1(text))
    assert "'from' needs a capture port; 'playback_1' goes the other way" in m
    assert any("'capture_0' is not a device port" in x for x in m)
    assert "'midi_capture' is a midi port but is listed under audio" in m
    assert any("'left' is not a device port" in x for x in m)
    assert any("hardware ID must be vendor:product" in x for x in m)


def test_id_collisions_and_unsupported_formats():
    text = DEVICES + "---\n" + rig(plugins=f"""
        - {{id: scarlett, lv2: {AMP}}}
        - {{id: a, lv2: {AMP}}}
        - {{id: a, lv2: {AMP}}}
        - {{id: v, vst3: /usr/lib/vst3/X.vst3}}
        """)
    m = msgs(t1(text))
    assert "plugin id 'scarlett' collides with a Device of the same name" in m
    assert "duplicate plugin id 'a' (also plugins[1])" in m
    assert "vst3 plugins aren't supported yet; this version hosts LV2 only" in m


def test_cycles_self_loops_and_duplicates():
    text = rig(
        plugins=f"""
        - {{id: a, lv2: {AMP}}}
        - {{id: b, lv2: {AMP}}}
        - {{id: c, lv2: {AMP}}}
        """,
        audio="""
        - {from: a:out, to: b:in}
        - {from: b:out, to: c:in}
        - {from: c:out, to: a:in}
        - {from: a:out, to: b:in}
        - {from: a:out, to: a:out}
        """,
    )
    ds = t1(text)
    assert "feedback loop: a → b → c → a" in msgs(ds)
    assert "a port can't connect to itself" in msgs(ds)
    assert msgs(ds, "warning") == ["duplicate connection (same as audio[0])"]


def test_device_round_trip_is_not_a_cycle():
    text = DEVICES + "---\n" + rig(
        plugins=f"- {{id: a, lv2: {AMP}}}",
        audio="- {from: scarlett:capture_1, to: a:in}\n- {from: a:out, to: scarlett:playback_1}",
    )
    assert t1(text) == []


def test_activerig_and_config_rules():
    text = (
        "apiVersion: r7s/v1\nkind: ActiveRig\nmetadata: {name: a}\nspec: {rig: nope}\n---\n"
        "apiVersion: r7s/v1\nkind: ActiveRig\nmetadata: {name: b}\nspec: {rig: nope}\n---\n"
        "apiVersion: r7s/v1\nkind: Config\nspec: {backend: mod-host}\n"
    )
    ds = t1(text)
    assert "only one ActiveRig allowed; also defined as ActiveRig/a" in msgs(ds)
    assert "no Rig named 'nope'" in msgs(ds, "warning")
    assert any("mod-host backend isn't implemented" in m for m in msgs(ds, "warning"))


@pytest.mark.parametrize(
    "params, expected",
    [
        ("gain: 30", "30 is outside gain [-24 .. 24 dB]"),
        ("gain: true", "gain is not a toggle; give a number in [-24 .. 24 dB]"),
        ("gain: loud", "gain takes a number in [-24 .. 24 dB]; labels only work on ports with named values"),
        ("bypass: 1", None),
        ("mode: 1.5", "mode takes whole numbers; got 1.5"),
        ("mode: Metal", "'Metal' is not a value of mode; choose one of: Clean, Crunch, Lead"),
        ("mode: crunch", None),
        ("gian: 1", "R7S Test Amp has no parameter 'gian'; did you mean 'gain'?"),
        ("level: 1", "'level' is a control output, not a control input"),
        ("in: 1", "'in' is an audio input, not a control input"),
        ("gain: {cc: 1, device: midimix, max: 99}", "CC max 99 is outside gain [-24 .. 24 dB]"),
        ("gain: {value: -30, cc: 1, device: midimix}", "-30 is outside gain [-24 .. 24 dB]"),
    ],
)
def test_parameter_checks(world, params, expected):
    text = DEVICES + "---\n" + rig(plugins=f"- id: amp\n  lv2: {AMP}\n  parameters:\n    {params}")
    assert msgs(t2(text, world)) == ([expected] if expected else [])


def test_plugin_port_checks(world):
    text = rig(
        plugins=f"- {{id: amp, lv2: {AMP}}}\n- {{id: amp2, lv2: {AMP}}}\n- {{id: synth, lv2: {SYNTH}}}",
        audio="""
        - {from: amp:in, to: synth:out_l}
        - {from: amp:gain, to: amp2:in}
        - {from: amp:outt, to: amp2:in}
        """,
        midi="""
        - {from: amp:out, to: synth:midi_in}
        - {from: amp2:out, to: synth:control}
        """,
    )
    m = msgs(t2(text, world))
    assert "'from' needs an output; amp:in is an input" in m
    assert "'to' needs an input; synth:out_l is an output" in m
    assert "amp:gain is a control input; audio connections need an audio port" in m
    assert "R7S Test Amp has no port 'outt'; audio outputs: out" in m
    assert "amp:out is an audio output; midi connections need a midi port" in m
    assert "synth:control is an atom input; midi connections need a midi port" in m


def test_missing_plugin_suggests(world):
    text = rig(plugins="- {id: amp, lv2: 'urn:r7s:test:ampp'}")
    assert msgs(t2(text, world)) == [
        "plugin 'urn:r7s:test:ampp' is not installed in the plugin paths; did you mean 'urn:r7s:test:amp'?"
    ]


def test_local_bundle(tmp_path):
    from r7s.lv2.world import PluginWorld
    from r7s.validate import rig_bundles

    text = rig(plugins=f"- {{id: amp, lv2: {AMP}, bundle: lv2/r7s-test.lv2}}")
    r = parse(text, base_dir=LV2_DIR.parent)
    world = PluginWorld.load([], extra_bundles=rig_bundles(r), cache_dir=False)
    assert [d for d in validate_environment(r, world) if d.severity == "error"] == []

    r = parse(rig(plugins=f"- {{id: amp, lv2: {AMP}, bundle: nowhere.lv2}}"), base_dir=tmp_path)
    world = PluginWorld.load([LV2_DIR], extra_bundles=rig_bundles(r), cache_dir=False)
    assert msgs(validate_environment(r, world))[0].endswith("not found (no manifest.ttl)")


def test_native_ui_requests_warn_and_run_headless():
    text = rig(plugins=f"- {{id: amp, lv2: {AMP}, ui: true}}\n- {{id: b, lv2: {AMP}, ui: false}}")
    ws = msgs(t1(text), "warning")
    assert len(ws) == 1 and "native plugin windows aren't supported yet" in ws[0]
    cfg = "apiVersion: r7s/v1\nkind: Config\nspec: {display: always}\n"
    assert any("native plugin windows" in m for m in msgs(t1(cfg), "warning"))
    assert msgs(t1(cfg.replace("always", "auto")), "warning") == []
