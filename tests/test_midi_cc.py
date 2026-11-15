import pytest

from r7s.hw.alsa import list_cards
from r7s.lv2.model import Port, ScalePoint
from r7s.midi_cc import CcEvent, CcRoute, CcRouter, RouteTable, coalesce, route_for, scale

from . import sysfake
from .test_midi_hw import seq
from .test_reconciler import AMP, Rig, rig_doc


def ctl(symbol="g", lo=0.0, hi=1.0, props=(), points=()):
    return Port(0, symbol, symbol, "input", "control", 0.0, lo, hi, None, list(props),
                [ScalePoint(label, v) for label, v in points])


SRC = (20, 0)


@pytest.mark.parametrize("port, value, expected", [
    (ctl(lo=-24, hi=24), 0, -24.0),
    (ctl(lo=-24, hi=24), 127, 24.0),
    (ctl(lo=0, hi=1), 64, 64 / 127),
    (ctl(lo=20, hi=20000, props=["logarithmic"]), 0, 20.0),
    (ctl(lo=20, hi=20000, props=["logarithmic"]), 127, 20000.0),
    (ctl(lo=20, hi=20000, props=["logarithmic"]), 64, pytest.approx(20 * 1000 ** (64 / 127))),
    (ctl(props=["toggled"]), 63, 0.0),
    (ctl(props=["toggled"]), 64, 1.0),
    (ctl(lo=0, hi=6, props=["integer"]), 64, 3.0),
    (ctl(lo=0, hi=2, props=["enumeration", "integer"], points=[("a", 0), ("b", 1), ("c", 2)]), 0, 0.0),
    (ctl(lo=0, hi=2, props=["enumeration", "integer"], points=[("a", 0), ("b", 1), ("c", 2)]), 60, 1.0),
    (ctl(lo=0, hi=2, props=["enumeration", "integer"], points=[("a", 0), ("b", 1), ("c", 2)]), 127, 2.0),
])
def test_scaling(port, value, expected):
    assert scale(route_for(port, SRC, 7, None, "p"), value) == expected


def test_min_max_override_and_log_needs_positive_range():
    r = route_for(ctl(lo=-30, hi=30), SRC, 7, 1, "p", lo=-6, hi=6)
    assert (scale(r, 0), scale(r, 127), r.channel) == (-6.0, 6.0, 0)
    assert route_for(ctl(lo=0, hi=1, props=["logarithmic"]), SRC, 7, None, "p").mode == "linear"


def test_channel_matching_and_coalescing():
    t = RouteTable()
    t.set([CcRoute(SRC, 0, 7, "a", "g", 0, 127), CcRoute(SRC, None, 7, "b", "g", 0, 127),
           CcRoute(SRC, None, 8, "a", "h", 0, 127)])
    evs = [CcEvent(SRC, 0, 7, 10), CcEvent(SRC, 3, 7, 20), CcEvent(SRC, 0, 8, 5),
           CcEvent(SRC, 0, 7, 30), CcEvent((21, 0), 0, 7, 99)]
    # Last-arrival order: the final CC 7 re-inserts a.g and b.g after a.h.
    assert list(coalesce(t, evs).items()) == [(("a", "h"), 5.0), (("a", "g"), 30.0), (("b", "g"), 30.0)]
    assert t.set(t.routes) is False  # unchanged


class FakeSeq:
    address = (130, 0)

    def __init__(self):
        self.subs, self.fail = set(), set()

    def subscribe(self, src):
        if src in self.fail:
            raise OSError("no such port")
        self.subs.add(src)

    def unsubscribe(self, src):
        self.subs.discard(src)

    def read(self, timeout):
        return []

    def close(self):
        pass


def test_router_syncs_subscriptions_and_retries():
    io = FakeSeq()
    io.fail.add((24, 0))
    got = []
    r = CcRouter(lambda *a: got.append(a), open_seq=lambda: io)
    r.update([CcRoute(SRC, None, 7, "a", "g", 0, 1), CcRoute((24, 0), None, 7, "a", "g", 0, 1)])
    r._sync_subscriptions(io)
    assert io.subs == {SRC} and (24, 0) in r._failed
    io.fail.clear()
    r._sync_subscriptions(io)
    assert io.subs == {SRC, (24, 0)}
    r.update([CcRoute((24, 0), None, 7, "a", "g", 0, 1)])
    r._sync_subscriptions(io)
    assert io.subs == {(24, 0)}
    r.deliver([CcEvent((24, 0), 0, 7, 127)])
    assert got == [("a", "g", 1.0)] and r.stats.events == 1 and r.stats.applied == 1


MIDIMIX = """
apiVersion: r7s/v1
kind: Device
metadata: {name: midimix}
spec: {usb: "09e8:0031"}
---
apiVersion: r7s/v1
kind: ActiveRig
metadata: {name: current}
spec: {rig: r}
"""
CC_RIG = rig_doc(plugins=f"[{{id: amp, lv2: '{AMP}', parameters: "
                         f"{{gain: {{value: 0, cc: 16, channel: 2, device: midimix, min: -6, max: 6}}, "
                         f"mode: {{cc: 17, device: midimix}}}}}}]")


@pytest.fixture
def cc(tmp_path):
    h = Rig(tmp_path / "state")
    sysfake.card(tmp_path / "sys", 1, "MIDImix", "usb", "09e8", "0031")
    h.cards = list_cards(tmp_path / "sys")
    h.seq = [seq(20, 0, "MIDI Mix MIDI 1", 1)]
    h.r.cc_router = CcRouter(h.r.apply_cc, open_seq=FakeSeq)  # not started: no I/O thread
    return h


def test_reconciler_builds_routes(cc):
    cc.put(MIDIMIX + "---" + CC_RIG)
    cc.sync()
    routes = sorted(cc.r.cc_router.table.routes, key=lambda r: r.cc)
    assert [(r.source, r.cc, r.channel, r.plugin, r.symbol, r.lo, r.hi, r.mode) for r in routes] == [
        ((20, 0), 16, 1, "amp", "gain", -6, 6, "linear"),
        ((20, 0), 17, None, "amp", "mode", 0.0, 2.0, "enum"),
    ]
    assert cc.r.status["midi"]["problems"] == []


def test_cc_changes_persist_until_spec_changes(cc):
    cc.put(MIDIMIX + "---" + CC_RIG)
    cc.sync()
    inst = cc.backend.latest("amp")
    cc.r.cc_router.sink("amp", "gain", 4.5)  # knob moved
    assert inst.sets == [("gain", 4.5)]
    cc.sync(3)
    assert inst.sets == [("gain", 4.5)]  # not reverted by later passes
    assert cc.r.live_params("amp")["gain"] == 4.5
    cc.put(CC_RIG.replace("value: 0", "value: 1"))
    cc.sync()
    assert inst.sets[-1] == ("gain", 1.0)  # a spec edit still wins


def test_unplugged_controller_reports_problem(cc):
    cc.cards = []
    cc.seq = []
    cc.put(MIDIMIX + "---" + CC_RIG)
    cc.sync()
    assert cc.r.cc_router.table.routes == ()
    problems = cc.r.status["midi"]["problems"]
    assert len(problems) == 2 and "usb device 09e8:0031 is not connected" in problems[0]
    assert any(e.reason == "CcUnmapped" for e in cc.r.events)


def _hw_diags(cc, text):
    from r7s.hw.inventory import Hardware
    from r7s.hw.pipewire import HwGraph
    from r7s.validate import validate_hardware

    from .conftest import parse

    r = parse(text)
    assert r.report.ok, r.report.diagnostics
    hw = Hardware(HwGraph.from_dump([]), cc.cards, cc.seq)
    return [d.message for d in validate_hardware(r, r, hw)]


def test_validate_hardware_cc_and_midi_sides(cc):
    assert _hw_diags(cc, MIDIMIX + "---" + CC_RIG) == []
    # The controller has MIDI ins but no audio: using its audio side warns.
    uses_audio = rig_doc(plugins=f"[{{id: amp, lv2: '{AMP}'}}]", audio="[{from: 'midimix:capture_1', to: 'amp:in'}]")
    assert _hw_diags(cc, MIDIMIX + "---" + uses_audio) == ["MIDImix has no capture audio endpoints"]
    cc.seq = [seq(20, 0, "out only", 1, source=False, sink=True)]
    assert _hw_diags(cc, MIDIMIX + "---" + CC_RIG) == [
        "MIDImix has no MIDI inputs; CC mappings and MIDI connections from it won't work"]
    cc.cards = []
    assert _hw_diags(cc, MIDIMIX + "---" + CC_RIG) == [
        "usb device 09e8:0031 is not connected; connections will wait for it"]
