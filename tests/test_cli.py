import json

import yaml
from click.testing import CliRunner

from r7s.cli import main

from .conftest import DEVICES, LV2_DIR, parse, rig

LV2 = ["--plugin-path", str(LV2_DIR)]


def run(*args, input=None):
    return CliRunner().invoke(main, list(args), input=input)


def test_plugins_list_json():
    res = run("plugins", "list", "-o", "json", *LV2)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert [p["uri"] for p in data] == ["urn:r7s:test:amp", "urn:r7s:test:synth"]
    assert data[1]["midi_in"] == 1 and data[1]["audio_out"] == 2


def test_plugins_search_and_inspect():
    res = run("plugins", "search", "synth", "-o", "uri", *LV2)
    assert res.output.strip() == "urn:r7s:test:synth"
    res = run("plugins", "inspect", "amp", *LV2)  # unambiguous search term
    assert res.exit_code == 0
    assert "values: Clean|Crunch|Lead" in res.output
    res = run("plugins", "inspect", "urn:nope", *LV2)
    assert res.exit_code == 1


def test_scaffold_output_validates_cleanly():
    res = run("plugins", "scaffold", "urn:r7s:test:amp", "--id", "amp", *LV2)
    assert res.exit_code == 0
    assert 'mode: "Crunch"' in res.output and "# Gain, [-24 .. 24 dB]" in res.output
    stanza = yaml.safe_load(res.output)
    assert stanza == [{
        "id": "amp",
        "lv2": "urn:r7s:test:amp",
        "parameters": {"gain": 0, "bypass": False, "mode": "Crunch"},
    }]
    doc = rig(plugins=res.output)
    res = run("validate", "--live", "-f", "-", *LV2, input=doc)
    assert res.exit_code == 0, res.output


def test_validate_exit_codes_and_json(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(DEVICES)
    assert run("validate", "-f", str(good)).exit_code == 0

    bad = tmp_path / "bad.yaml"
    bad.write_text(rig(plugins="- {id: amp, lv2: 'urn:x'}", audio="- {from: amp:out, to: amp:out}"))
    res = run("validate", "--live", "-o", "json", "-f", str(bad), *LV2)
    assert res.exit_code == 1
    data = json.loads(res.output)
    assert data["valid"] is False and data["tiers"] == [1, 2]
    assert {d["path"] for d in data["diagnostics"]} >= {"spec.connections.audio[0]", "spec.plugins[0].lv2"}

    assert run("validate", "-f", str(tmp_path / "missing.yaml")).exit_code == 2


def test_plugin_paths_from_config_resource(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"apiVersion: r7s/v1\nkind: Config\nspec: {{pluginPaths: [{LV2_DIR}]}}\n")
    r = tmp_path / "rig.yaml"
    r.write_text(rig(plugins="- {id: amp, lv2: 'urn:r7s:test:amp'}"))
    res = run("validate", "--live", "-f", str(cfg), "-f", str(r))
    assert res.exit_code == 0, res.output


def test_example_rig_is_tier1_valid():
    res = run("validate", "-f", "examples/rig")
    assert res.exit_code == 0, res.output
    assert parse  # keep import used


def test_beatstep_example_is_tier1_valid():
    res = run("validate", "-f", "examples/beatstep-303/rig.yaml")
    assert res.exit_code == 0, res.output
