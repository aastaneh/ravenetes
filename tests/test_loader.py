import pytest

from r7s.loader import InputError, load
from r7s.schema import ParamMapping

from .conftest import DEVICES, parse, rig, stdin


def errors(result):
    return [(d.line, str(d.path and ".".join(map(str, d.path))), d.message)
            for d in result.report.diagnostics if d.severity == "error"]


def test_multi_doc_stream():
    r = parse(DEVICES + "---\n" + rig())
    assert [x.ref for x in r.resources] == ["Device/scarlett", "Device/midimix", "Rig/r"]
    assert r.resources[1].resource.spec.usb == "09e8:0031"  # normalized
    assert r.report.ok


def test_config_metadata_is_optional():
    r = parse("apiVersion: r7s/v1\nkind: Config\nspec: {pluginPaths: [/x]}\n")
    assert r.resources[0].name == "default"
    assert r.resources[0].resource.spec.plugin_paths == ["/x"]


def test_yaml_syntax_error_has_line():
    r = parse("apiVersion: r7s/v1\nkind: Rig\nmetadata: {name: [\n")
    (d,) = r.report.diagnostics
    assert d.message.startswith("YAML syntax error") and d.line is not None


def test_unknown_kind_and_missing_kind():
    r = parse("apiVersion: r7s/v1\nkind: Rigg\nmetadata: {name: x}\n---\nfoo: 1\n")
    msgs = [d.message for d in r.report.diagnostics]
    assert "unknown kind 'Rigg'" in msgs[0]
    assert msgs[1] == "missing 'kind'"


def test_schema_errors_point_at_field_lines():
    text = rig(plugins="""
        - id: amp
          lv2: urn:r7s:test:amp
          parameters:
            gain: {value: 1, channel: 2}
            mode: [1, 2]
          colour: red
        """)
    r = parse(text)
    errs = errors(r)
    lines = text.splitlines()
    by_path = {p: (line, m) for line, p, m in errs}
    line, msg = by_path["spec.plugins.0.parameters.gain"]
    assert "channel only applies to a CC mapping" in msg
    assert "gain:" in lines[line - 1]
    line, msg = by_path["spec.plugins.0.parameters.mode"]
    assert "expected a number, a boolean, a scale-point label, or a mapping" in msg
    assert by_path["spec.plugins.0.colour"][1] == "unknown field"


@pytest.mark.parametrize(
    "spec, message",
    [
        ("{}", "set exactly one of 'usb', 'pci', or 'alsa'"),
        ('{usb: "1235:8210", pci: "8086:a171"}', "set exactly one of 'usb', 'pci', or 'alsa'"),
        ('{usb: "1235-8210"}', "vendor:product"),
    ],
)
def test_device_spec_errors(spec, message):
    r = parse(f"apiVersion: r7s/v1\nkind: Device\nmetadata: {{name: d}}\nspec: {spec}\n")
    assert message in r.report.diagnostics[0].message


def test_plugin_format_rules():
    r = parse(rig(plugins="""
        - {id: a}
        - {id: b, lv2: x, clap: y}
        - {id: c, vst3: /x.vst3, bundle: ./x}
        """))
    msgs = [m for _, _, m in errors(r)]
    assert msgs.count("set exactly one plugin format: 'lv2', 'vst3', or 'clap'") == 2
    assert "'bundle' only applies to lv2 plugins" in msgs


def test_param_value_forms():
    r = parse(rig(plugins="""
        - id: amp
          lv2: urn:r7s:test:amp
          parameters:
            gain: 3
            bypass: true
            mode: Lead
            other: {cc: 7, device: midimix}
        """))
    assert r.report.ok, r.report.diagnostics
    params = r.resources[0].resource.spec.plugins[0].parameters
    assert params["gain"] == 3.0
    assert params["bypass"] is True
    assert params["mode"] == "Lead"
    assert isinstance(params["other"], ParamMapping) and params["other"].value is None


def test_cc_mapping_needs_device():
    r = parse(rig(plugins="- {id: a, lv2: x, parameters: {g: {cc: 7}}}"))
    assert "needs 'device'" in r.report.diagnostics[0].message


def test_duplicates_across_files(tmp_path):
    (tmp_path / "a.yaml").write_text(DEVICES)
    (tmp_path / "b.yaml").write_text(DEVICES.split("---")[0])
    r = load([str(tmp_path)])
    (d,) = r.report.diagnostics
    assert "duplicate Device/scarlett" in d.message and "a.yaml:1" in d.message
    assert len(r.resources) == 2


def test_directory_recursion_and_stdin(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.yml").write_text(DEVICES)
    (tmp_path / "notes.txt").write_text("ignored")
    with pytest.raises(InputError, match="-R"):
        load([str(tmp_path)])
    assert len(load([str(tmp_path)], recursive=True).resources) == 2
    assert len(load(["-"], stdin=stdin(rig())).resources) == 1
    with pytest.raises(InputError, match="no such file"):
        load([str(tmp_path / "missing.yaml")])


def test_yaml12_booleans_keep_port_symbols_as_strings():
    r = parse(rig(plugins="- {id: a, lv2: x, parameters: {on: 1, off: true, yes: no}}"))
    assert r.report.ok, r.report.diagnostics
    params = r.resources[0].resource.spec.plugins[0].parameters
    assert params == {"on": 1.0, "off": True, "yes": "no"}
