import yaml
import pytest

from r7s.backend.fake import FakeBackend
from r7s.hw.inventory import Hardware
from r7s.hw.pipewire import HwGraph, PipeWireError
from r7s.jackgraph import FakeGraph
from r7s.lv2.world import PluginWorld
from r7s.reconciler import Reconciler
from r7s.store import Store

from . import pwfake
from .conftest import LV2_DIR

AMP = "urn:r7s:test:amp"
SYNTH = "urn:r7s:test:synth"
SCARLETT_IN = "Scarlett 2i2 USB Analog Stereo:capture_FL"
SCARLETT_OUT_L = "Scarlett 2i2 USB Analog Stereo:playback_FL"
SCARLETT_OUT_R = "Scarlett 2i2 USB Analog Stereo:playback_FR"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    """Harness: a store, fake JACK graph with a Scarlett, and a reconciler."""

    def __init__(self, tmp_path):
        self.store = Store(tmp_path)
        self.store.load()
        self.world = PluginWorld.load([LV2_DIR], cache_dir=False)
        self.graph = FakeGraph()
        self.hw_dump = pwfake.scarlett()
        self.plug_scarlett(True)
        self.backend = FakeBackend(self.world, self.graph)
        self.clock = Clock()
        self.hw_fail = False
        self.cards = None  # sysfs cards (None: unknown)
        self.seq = None  # sequencer ports (None: unknown)
        self.r = Reconciler(
            self.store,
            make_backend=lambda res: self.backend,
            make_graph=lambda cb: self.graph,
            make_world=lambda res: self.world,
            hw_snapshot=self.snapshot,
            clock=self.clock,
        )

    def snapshot(self):
        if self.hw_fail:
            raise PipeWireError("pw-dump not found")
        return Hardware(HwGraph.from_dump(self.hw_dump), self.cards, self.seq)

    def plug_scarlett(self, on: bool):
        client = "Scarlett 2i2 USB Analog Stereo"
        if on:
            self.graph.add_client(client, {"capture_FL": "audio", "capture_FR": "audio",
                                           "playback_FL": "audio", "playback_FR": "audio"})
            self.hw_dump = pwfake.scarlett()
        else:
            self.graph.remove_client(client)
            self.hw_dump = []

    def put(self, text: str):
        for doc in yaml.safe_load_all(text):
            self.store.put(doc["kind"], doc.get("metadata", {}).get("name", "default"), doc, "test")

    def sync(self, passes: int = 2):
        for _ in range(passes):
            self.r._hw_dirty = True
            self.r.reconcile_once()
        return self.r.status

    def rig_status(self, name="r"):
        return self.r.status["rigs"][name]


DEVICE = """
apiVersion: r7s/v1
kind: Device
metadata: {name: scarlett}
spec: {usb: "1235:8210"}
---
apiVersion: r7s/v1
kind: ActiveRig
metadata: {name: current}
spec: {rig: r}
"""


def rig_doc(name="r", plugins="", audio="", midi=""):
    return f"""
apiVersion: r7s/v1
kind: Rig
metadata: {{name: {name}}}
spec:
  plugins: {plugins or "[]"}
  connections:
    audio: {audio or "[]"}
    midi: {midi or "[]"}
"""


BASIC = rig_doc(
    plugins=f"[{{id: amp, lv2: '{AMP}', parameters: {{gain: 6, mode: Lead}}}}]",
    audio="[{from: 'scarlett:capture_1', to: 'amp:in'}, {from: 'amp:out', to: 'scarlett:playback_l'},"
          " {from: 'amp:out', to: 'scarlett:playback_r'}]",
)


@pytest.fixture
def h(tmp_path):
    return Rig(tmp_path)


def test_bring_up(h):
    h.put(DEVICE + "---" + BASIC)
    st = h.sync()
    inst = h.backend.latest("amp")
    assert inst.spec.client == "r7s-amp"
    assert inst.launch_params["gain"] == 6.0 and inst.launch_params["mode"] == 2.0
    assert "bypass" not in inst.launch_params  # default, not explicit
    assert h.graph.edges == {
        (SCARLETT_IN, "r7s-amp:in"), ("r7s-amp:out", SCARLETT_OUT_L), ("r7s-amp:out", SCARLETT_OUT_R)}
    rs = h.rig_status()
    assert rs["phase"] == "Ready" and rs["active"] and rs["plugins"]["amp"]["state"] == "Running"
    assert st["devices"]["scarlett"]["present"] is True
    assert inst.sets == []  # launch params already applied


def test_param_change_is_live_not_restart(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.put(BASIC.replace("gain: 6", "gain: -3"))
    h.sync()
    assert len(h.backend.instances) == 1
    assert h.backend.latest("amp").sets == [("gain", -3.0)]


def test_removed_param_returns_to_default(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.put(BASIC.replace("gain: 6, mode: Lead", "mode: Lead"))
    h.sync()
    assert h.backend.latest("amp").sets == [("gain", 0.0)]


def test_live_tweak_persists_until_spec_changes(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    inst = h.backend.latest("amp")
    inst.params["gain"] = 12.0  # someone turned the knob in a native UI
    h.sync()
    assert inst.sets == []  # not reverted
    h.put(BASIC.replace("gain: 6", "gain: 3"))
    h.sync()
    assert inst.sets == [("gain", 3.0)]


def test_uri_change_restarts_only_that_plugin(h):
    two = rig_doc(plugins=f"[{{id: a, lv2: '{AMP}'}}, {{id: b, lv2: '{AMP}'}}]",
                  audio="[{from: 'a:out', to: 'b:in'}]")
    h.put(DEVICE + "---" + two)
    h.sync()
    a1 = h.backend.latest("a")
    h.put(two.replace(f"{{id: b, lv2: '{AMP}'}}", f"{{id: b, lv2: '{SYNTH}'}}"))
    h.sync()
    assert h.backend.latest("a") is a1 and a1.alive()
    assert h.backend.stopped == ["b"]
    assert h.backend.latest("b").spec.uri == SYNTH
    # a:out -> b:in no longer resolves to an existing port on the synth
    assert h.rig_status()["connections"][0]["state"] == "Pending"


def test_crash_restart_with_backoff_and_edge_restore(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.backend.latest("amp").crash(-11)
    h.sync(1)
    rs = h.rig_status()
    assert rs["plugins"]["amp"]["state"] == "CrashLoop" and rs["phase"] == "Degraded"
    assert rs["plugins"]["amp"]["restarts"] == 1
    assert len(h.backend.instances) == 1  # still backing off
    h.clock.t += 1.5
    h.sync()
    assert len(h.backend.instances) == 2 and h.backend.latest("amp").alive()
    assert (SCARLETT_IN, "r7s-amp:in") in h.graph.edges
    reasons = [e.reason for e in h.r.events]
    assert "Crashed" in reasons and "Restored" in reasons
    # Second crash backs off longer.
    h.backend.latest("amp").crash()
    h.sync(1)
    h.clock.t += 1.5
    h.sync()
    assert len(h.backend.instances) == 2
    h.clock.t += 1.0
    h.sync()
    assert len(h.backend.instances) == 3


def test_external_disconnect_is_restored(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.graph.disconnect("r7s-amp:out", SCARLETT_OUT_L)
    h.sync()
    assert ("r7s-amp:out", SCARLETT_OUT_L) in h.graph.edges
    assert any(e.reason == "Restored" for e in h.r.events)


def test_foreign_edges_are_left_alone(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.graph.add_client("browser", {"out_l": "audio"})
    h.graph.connect("browser:out_l", SCARLETT_OUT_L)
    h.graph.connect("browser:out_l", "r7s-amp:in")  # even onto r7s ports
    h.put(BASIC.replace(", {from: 'amp:out', to: 'scarlett:playback_r'}", ""))
    h.sync()
    assert ("browser:out_l", SCARLETT_OUT_L) in h.graph.edges
    assert ("browser:out_l", "r7s-amp:in") in h.graph.edges
    assert ("r7s-amp:out", SCARLETT_OUT_R) not in h.graph.edges  # ours, undeclared


def test_rig_switch_keeps_matching_instances(h):
    other = rig_doc(name="r2", plugins=f"[{{id: amp, lv2: '{AMP}', parameters: {{bypass: true}}}},"
                                       f" {{id: synth, lv2: '{SYNTH}'}}]",
                    audio="[{from: 'synth:out_l', to: 'amp:in'}]")
    h.put(DEVICE + "---" + BASIC + "---" + other)
    h.sync()
    amp = h.backend.latest("amp")
    h.put("apiVersion: r7s/v1\nkind: ActiveRig\nmetadata: {name: current}\nspec: {rig: r2}\n")
    h.sync()
    assert h.backend.latest("amp") is amp  # same id + uri: no restart
    assert sorted(amp.sets) == [("bypass", 1.0), ("gain", 0.0), ("mode", 1.0)]
    assert h.graph.edges == {("r7s-synth:out_l", "r7s-amp:in")}
    assert h.r.status["rigs"]["r"]["phase"] == "Inactive"
    assert h.r.status["rigs"]["r2"]["phase"] == "Ready"


def test_device_unplug_and_replug(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.plug_scarlett(False)
    h.sync()
    rs = h.rig_status()
    assert rs["phase"] == "Degraded"
    unresolved = [c for c in rs["connections"] if c["state"] == "Unresolved"]
    assert len(unresolved) == 3
    assert "usb device 1235:8210 is not connected" in unresolved[0]["reason"]
    assert h.r.status["devices"]["scarlett"]["present"] is False
    h.plug_scarlett(True)
    h.sync()
    assert h.rig_status()["phase"] == "Ready" and len(h.graph.edges) == 3


def test_no_pipewire_degrades_device_edges_only(h):
    h.hw_fail = True
    h.put(DEVICE + "---" + rig_doc(
        plugins=f"[{{id: a, lv2: '{AMP}'}}, {{id: b, lv2: '{AMP}'}}]",
        audio="[{from: 'a:out', to: 'b:in'}, {from: 'b:out', to: 'scarlett:playback_1'}]"))
    h.sync()
    conns = h.rig_status()["connections"]
    assert conns[0]["state"] == "Connected"
    assert conns[1]["state"] == "Unresolved" and "pw-dump not found" in conns[1]["reason"]


def test_deactivate_stops_everything(h):
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    h.store.delete("ActiveRig", "current")
    h.sync()
    assert h.backend.stopped == ["amp"] and h.graph.edges == set()
    assert h.r.status["activeRig"] is None


def test_start_failure_backs_off(h):
    h.backend.fail_start.add("amp")
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    st = h.rig_status()["plugins"]["amp"]
    assert st["state"] == "Waiting" and "cannot start amp" in st["lastError"]
    h.backend.fail_start.clear()
    h.clock.t += 1.1
    h.sync()
    assert h.rig_status()["plugins"]["amp"]["state"] == "Running"
