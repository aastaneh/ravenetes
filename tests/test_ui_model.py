from r7s.ui.model import build_graph

from .test_reconciler import AMP, DEVICE, SYNTH, Rig, rig_doc
import pytest

RIG = rig_doc(
    plugins=f"[{{id: amp, lv2: '{AMP}', parameters: {{gain: 6, mode: Lead}}}}, {{id: synth, lv2: '{SYNTH}'}}]",
    audio="[{from: 'scarlett:capture_1', to: 'amp:in'}, {from: 'amp:out', to: 'scarlett:playback_l'},"
          " {from: 'synth:out_l', to: 'scarlett:playback_r'}]",
)


@pytest.fixture
def h(tmp_path):
    h = Rig(tmp_path)
    h.put(DEVICE + "---" + RIG)
    h.sync()
    return h


def test_graph_nodes_and_edges(h):
    g = build_graph(h.r, h.store)
    assert g["rig"] == "r" and g["active"] and g["phase"] == "Ready" and g["rigs"] == ["r"]
    ids = {n["id"]: n for n in g["nodes"]}
    assert set(ids) == {"p:amp", "p:synth", "d:scarlett:in", "d:scarlett:out"}
    assert ids["d:scarlett:in"]["outputs"] == [{"symbol": "capture_1", "name": "capture_1", "type": "audio"}]
    assert {p["symbol"] for p in ids["d:scarlett:out"]["inputs"]} == {"playback_l", "playback_r"}
    assert ids["d:scarlett:in"]["present"] is True
    amp = ids["p:amp"]
    assert amp["state"] == "Running" and amp["title"] == "R7S Test Amp" and amp["pid"]
    assert [p["symbol"] for p in amp["inputs"]] == ["in"] and [p["symbol"] for p in amp["outputs"]] == ["out"]
    assert [(e["from"], e["fromPort"], e["to"], e["toPort"], e["state"]) for e in g["edges"]] == [
        ("d:scarlett:in", "capture_1", "p:amp", "in", "Connected"),
        ("p:amp", "out", "d:scarlett:out", "playback_l", "Connected"),
        ("p:synth", "out_l", "d:scarlett:out", "playback_r", "Connected"),
    ]


def test_params_show_live_spec_and_drift(h):
    params = {p["symbol"]: p for p in next(n for n in build_graph(h.r, h.store)["nodes"] if n["id"] == "p:amp")["params"]}
    assert params["gain"]["spec"] == 6.0 and params["gain"]["live"] == 6.0 and not params["gain"]["drift"]
    assert params["mode"]["live"] == "Lead" and params["mode"]["labels"] == ["Clean", "Crunch", "Lead"]
    assert params["bypass"]["toggle"] and params["bypass"]["live"] is False and not params["bypass"]["inSpec"]
    h.r.apply_cc("amp", "gain", 2.5)
    g = build_graph(h.r, h.store)
    amp = next(n for n in g["nodes"] if n["id"] == "p:amp")
    gain = next(p for p in amp["params"] if p["symbol"] == "gain")
    assert gain["live"] == 2.5 and gain["applied"] == 6.0 and gain["drift"]
    assert amp["drift"] == 1 and g["drift"] == 1


def test_inactive_rig_and_unplugged_device(h):
    h.put(rig_doc(name="other", plugins=f"[{{id: x, lv2: '{AMP}'}}]"))
    h.sync()
    g = build_graph(h.r, h.store, "other")
    assert g["rig"] == "other" and not g["active"] and g["phase"] == "Inactive"
    assert g["nodes"][0]["state"] == "Inactive" and g["rigs"] == ["other", "r"]
    h.plug_scarlett(False)
    h.sync()
    g = build_graph(h.r, h.store)
    dev = next(n for n in g["nodes"] if n["id"] == "d:scarlett:in")
    assert dev["present"] is False and "not connected" in dev["errors"]["device"]
    assert {e["state"] for e in g["edges"] if "d:scarlett" in (e["from"] + e["to"])} == {"Unresolved"}
    assert g["phase"] == "Degraded"


def test_no_active_rig(h):
    h.store.delete("ActiveRig", "current")
    h.sync()
    g = build_graph(h.r, h.store)
    assert g["rig"] is None and g["nodes"] == [] and g["rigs"] == ["r"]
