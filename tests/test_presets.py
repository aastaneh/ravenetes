import os
import stat
import time

import pytest

from r7s.backend.base import InstanceSpec
from r7s.backend.jalv import JalvInstance
from r7s.desired import compute_desired
from r7s.lv2.ttl import scan_bundle
from r7s.lv2.world import PluginWorld

from .conftest import LV2_DIR, parse, rig
from .test_reconciler import AMP, DEVICE, Rig
from .test_validate import msgs, t2

PRESET_BUNDLE = LV2_DIR / "r7s-test-amp_crunch.preset.lv2"
CRUNCH_URI = (PRESET_BUNDLE / "crunch.ttl").resolve().as_uri()


def test_scan_finds_file_fragment_and_broken_presets(world: PluginWorld):
    only = scan_bundle(PRESET_BUNDLE)
    assert only.plugins == [] and [p.label for p in only.presets] == ["Crunch Rhythm"]
    crunch = world.presets[CRUNCH_URI]
    assert crunch.path == str((PRESET_BUNDLE / "crunch.ttl").resolve())
    assert crunch.port_values == {"gain": 9.0, "mode": 1.0} and crunch.error is None
    clean = world.presets["urn:r7s:test:amp#clean"]
    assert clean.path is None and clean.port_values == {"gain": -6.0, "mode": 0.0}
    broken = world.presets["urn:r7s:test:amp#preset1"]
    assert broken.error and "broken-preset.ttl" in broken.error
    assert {p.uri for p in world.presets_for(AMP)} == {CRUNCH_URI, "urn:r7s:test:amp#clean", "urn:r7s:test:amp#preset1"}


@pytest.mark.parametrize("ref, ok", [
    ("Crunch Rhythm", True), ("crunch rhythm", True), (CRUNCH_URI, True), ("urn:r7s:test:amp#clean", True),
    ("Crunch", False), ("urn:nope", False),
])
def test_find_preset(world, ref, ok):
    preset, why = world.find_preset(AMP, ref)
    assert (preset is not None) is ok and (why is None) is ok


def test_find_preset_wrong_plugin(world):
    preset, why = world.find_preset("urn:r7s:test:synth", "urn:r7s:test:amp#clean")
    assert preset is None and "belongs to urn:r7s:test:amp" in why


def test_validation(world):
    good = rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: Crunch Rhythm}}")
    assert t2(good, world) == []
    assert msgs(t2(rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: Crunchy Rhythm}}"), world)) == [
        "no preset 'Crunchy Rhythm' for this plugin; did you mean 'Crunch Rhythm'?"]
    broken = msgs(t2(rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: 'urn:r7s:test:amp#preset1'}}"), world))
    assert len(broken) == 1 and "can't be read" in broken[0]
    r = parse(rig(plugins="- {id: v, vst3: /x.vst3, preset: x}"))
    assert "'preset' only applies to lv2 plugins" in r.report.diagnostics[0].message


def test_desired_baseline_and_launch(world):
    r = parse(DEVICE + "---\n" + rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: Crunch Rhythm, parameters: {{mode: Lead}}}}"))
    d = compute_desired(r, world, None)
    want = d.plugins["amp"]
    assert want.spec.preset_path == str((PRESET_BUNDLE / "crunch.ttl").resolve()) and want.spec.preset_uri is None
    assert want.baseline["gain"] == 9.0 and want.params["gain"] == 9.0  # from the preset
    assert want.params["mode"] == 2.0  # explicit "Lead" overrides the preset's Crunch
    assert want.launch_params() == {"mode": 2.0}  # gain already 9 via the preset: not re-sent
    assert want.preset_label == "Crunch Rhythm"
    r = parse(DEVICE + "---\n" + rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: Clean}}"))
    spec = compute_desired(r, world, None).plugins["amp"].spec
    assert spec.preset_uri == "urn:r7s:test:amp#clean" and spec.preset_path is None


def test_reconciler_launches_from_preset_and_restarts_on_change(tmp_path):
    h = Rig(tmp_path)
    base = DEVICE + "---\n" + rig(plugins=f"- {{id: amp, lv2: {AMP}, preset: Crunch Rhythm}}")
    h.put(base)
    h.sync()
    first = h.backend.latest("amp")
    assert first.target.endswith("crunch.ttl") and first.launch_params == {}
    h.r.apply_cc("amp", "gain", 1.0)
    h.sync()
    assert h.backend.latest("amp") is first  # unrelated passes don't restart
    h.put(base.replace("preset: Crunch Rhythm", "preset: Clean"))
    h.sync()
    second = h.backend.latest("amp")
    assert second is not first and h.backend.stopped == ["amp"]
    assert second.target == AMP and second.spec.preset_uri == "urn:r7s:test:amp#clean"


def _fake_jalv(tmp_path):
    log = tmp_path / "log"
    exe = tmp_path / "jalv"
    exe.write_text(f'#!/bin/sh\nprintf "ARGS %s\\n" "$*" > {log}\nwhile read line; do echo "IN $line" >> {log}; '
                   f'[ "$line" = quit ] && exit 0; done\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe), log


def _lines(log, n):
    for _ in range(100):
        if log.exists() and len(log.read_text().splitlines()) >= n:
            break
        time.sleep(0.02)
    return log.read_text().splitlines()


def test_jalv_gets_path_for_file_presets(tmp_path):
    exe, log = _fake_jalv(tmp_path)
    inst = JalvInstance(exe, InstanceSpec("d", "urn:x", preset_path="/p/kit.ttl"), {"enabled": 1.0}, dict(os.environ))
    lines = _lines(log, 1)
    inst.stop(timeout=1)
    assert lines[0] == "ARGS -n r7s-d -x -c enabled=1 /p/kit.ttl"


def test_jalv_applies_fragment_presets_over_stdin_then_our_values(tmp_path):
    exe, log = _fake_jalv(tmp_path)
    inst = JalvInstance(exe, InstanceSpec("a", "urn:x", preset_uri="urn:x#clean"), {"gain": 3.0}, dict(os.environ))
    lines = _lines(log, 3)
    inst.stop(timeout=1)
    assert lines[:3] == ["ARGS -n r7s-a -x -c gain=3 urn:x", "IN preset urn:x#clean", "IN set gain 3"]
