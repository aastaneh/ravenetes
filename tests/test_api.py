import os
import threading

import pytest
import yaml

from r7s.api import Api
from r7s.client import Client, DaemonUnavailable
from r7s.daemon import UnixHTTPServer, _claim_socket, make_handler

from . import pwfake
from .test_reconciler import AMP, DEVICE, Rig, rig_doc

BASIC = rig_doc(
    plugins=f"[{{id: amp, lv2: '{AMP}', parameters: {{gain: 6}}}}]",
    audio="[{from: 'scarlett:capture_1', to: 'amp:in'}]",
)


def docs(text: str, source="rig.yaml"):
    return [{"data": d, "source": source, "lines": [[["kind"], 3]]} for d in yaml.safe_load_all(text) if d]


@pytest.fixture
def env(tmp_path):
    h = Rig(tmp_path)
    api = Api(h.store, h.r, make_world=lambda res: h.world, hw_snapshot=h.snapshot)
    return h, api


def test_apply_create_unchanged_configured(env):
    h, api = env
    code, resp = api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    assert code == 200, resp
    assert [r["action"] for r in resp["results"]] == ["created", "created", "created"]
    assert resp["revision"] == h.store.revision
    code, resp = api.apply({"documents": docs(BASIC)})
    assert resp["results"] == [{"ref": "Rig/r", "action": "unchanged"}]
    code, resp = api.apply({"documents": docs(BASIC.replace("gain: 6", "gain: 3"))})
    assert resp["results"] == [{"ref": "Rig/r", "action": "configured"}]
    assert h.store.get("Rig", "r").generation == 2


def test_apply_uses_store_for_references(env):
    h, api = env
    # Rig alone references a Device that isn't stored yet: strict error.
    code, resp = api.apply({"documents": docs(BASIC)})
    assert code == 422
    assert any("not a plugin in this rig or a known Device" in d["message"] for d in resp["diagnostics"])
    assert h.store.list() == []
    # Once the Device is stored, the same Rig is accepted.
    api.apply({"documents": docs(DEVICE.split("---")[0])})
    assert api.apply({"documents": docs(BASIC)})[0] == 200


def test_apply_tier2_errors_keep_source_lines(env):
    h, api = env
    code, resp = api.apply({"documents": docs(DEVICE + "---" + BASIC.replace("gain: 6", "gain: 99"))})
    assert code == 422
    (d,) = [d for d in resp["diagnostics"] if d["severity"] == "error"]
    assert d["message"] == "99 is outside gain [-24 .. 24 dB]" and d["tier"] == 2
    assert d["source"] == "rig.yaml"


def test_dry_run_stores_nothing(env):
    h, api = env
    code, resp = api.apply({"documents": docs(DEVICE + "---" + BASIC), "dryRun": True})
    assert code == 200 and resp["dryRun"] and h.store.list() == []


def test_hw_warning_only_for_used_directions(env):
    h, api = env
    h.hw_dump = pwfake.umc404_pro_audio() + pwfake.scarlett("AAA") + pwfake.scarlett("BBB")
    code, resp = api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    assert code == 200
    msgs = [d["message"] for d in resp["diagnostics"]]
    assert any("2 devices match" in m and "connections will wait" in m for m in msgs)


def test_ambiguous_side_warns_only_when_used(env):
    h, api = env
    import json
    from .conftest import FIXTURES
    h.hw_dump = json.loads((FIXTURES / "pw" / "laptop-hda.json").read_text())
    mic = ('apiVersion: r7s/v1\nkind: Device\nmetadata: {name: mic}\n'
           'spec: {pci: "8086:51c8", capture: Mic2}\n')
    uses_mic = rig_doc(plugins=f"[{{id: amp, lv2: '{AMP}'}}]", audio="[{from: 'mic:capture_1', to: 'amp:in'}]")
    code, resp = api.apply({"documents": docs(mic + "---" + uses_mic)})
    assert code == 200 and resp["diagnostics"] == []
    uses_out = uses_mic.replace("audio: [{from: 'mic:capture_1', to: 'amp:in'}]",
                                "audio: [{from: 'amp:out', to: 'mic:playback_1'}]")
    code, resp = api.apply({"documents": docs(uses_out)})
    assert any("4 playback endpoints" in d["message"] for d in resp["diagnostics"])


def test_delete_guards_references(env):
    h, api = env
    api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    code, resp = api.delete([{"kind": "rig", "name": "r"}])
    assert code == 422 and "dangling" in resp["error"]
    code, resp = api.delete([{"kind": "rig", "name": "r"}, {"kind": "ActiveRig", "name": "current"}])
    assert code == 200 and h.store.get("Rig", "r") is None
    assert api.delete([{"kind": "rig", "name": "r"}])[0] == 404


def test_get_list_and_status(env):
    h, api = env
    api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    h.sync()
    code, d = api.get("rigs", "r")
    assert code == 200 and d["status"]["phase"] == "Ready"
    assert any(e["reason"] == "Started" for e in d["events"])
    code, d = api.list("devices")
    assert [i["name"] for i in d["items"]] == ["scarlett"] and d["items"][0]["status"]["present"]
    assert api.list("bogus")[0] == 400
    code, st = api.status()
    assert st["activeRig"] == "r" and st["reconciledRevision"] == h.store.revision and not st["starting"]


def test_diff(env):
    h, api = env
    api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    code, d = api.diff({"documents": docs(BASIC.replace("gain: 6", "gain: 1"))})
    (item,) = d["items"]
    assert item["state"] == "changed" and "-" in item["diff"] and "gain: 1" in item["diff"]
    code, d = api.diff({"documents": docs(BASIC)})
    assert d["items"][0] == {"ref": "Rig/r", "state": "unchanged", "diff": ""}


def test_snapshot_reports_live_values(env):
    h, api = env
    api.apply({"documents": docs(DEVICE + "---" + BASIC)})
    h.sync()
    # A MIDI CC moved these knobs: reflected in "live".
    h.r.managed["amp"].live["gain"] = -2.0
    h.r.managed["amp"].live["mode"] = 2.0
    code, d = api.snapshot("r")
    params = d["manifest"]["spec"]["plugins"][0]["parameters"]
    assert params == {"gain": -2.0, "mode": "Lead"}
    assert sorted(d["changed"]) == ["amp.gain", "amp.mode"]
    api.delete([{"kind": "ActiveRig", "name": "current"}])
    h.sync()
    assert api.snapshot("r")[0] == 409


def test_socket_round_trip(env, tmp_path):
    h, api = env
    path = str(tmp_path / "r7s.sock")
    _claim_socket(path)
    server = UnixHTTPServer(path, make_handler(api))
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        c = Client(path, timeout=5)
        code, resp = c.post("/v1/apply", {"documents": docs(DEVICE + "---" + BASIC)})
        assert code == 200 and len(resp["results"]) == 3
        code, resp = c.get("/v1/resources?kind=rig")
        assert [i["name"] for i in resp["items"]] == ["r"]
        assert c.get("/v1/nope")[0] == 404
        # A second daemon on the same socket is refused.
        with pytest.raises(SystemExit, match="already listening"):
            _claim_socket(path)
    finally:
        server.shutdown()
        server.server_close()
    # Stale socket (no listener) is reclaimed.
    assert os.path.exists(path)
    _claim_socket(path)
    assert not os.path.exists(path)
    with pytest.raises(DaemonUnavailable, match="r7sd is not running"):
        Client(path, timeout=1).get("/v1/status")


