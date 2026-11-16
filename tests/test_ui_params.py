import time

from r7s.ui.server import Broadcaster, ParamFeed, display_value

from .test_reconciler import BASIC, DEVICE, Rig


def test_feed_coalesces_to_latest_value():
    b = Broadcaster()
    cond, q = b.subscribe()
    feed = ParamFeed(b, display=lambda p, s, v: f"{v:g}!")
    for v in (1.0, 2.0, 3.0):
        feed.push("synth", "cutoff", v)
    feed.push("synth", "resonance", 9.0)
    sent = feed.flush()
    assert sent == [{"plugin": "synth", "symbol": "cutoff", "value": 3.0, "shown": "3!"},
                    {"plugin": "synth", "symbol": "resonance", "value": 9.0, "shown": "9!"}]
    assert list(q) == [("params", sent)] and feed.flush() == []


def test_feed_thread_rate_limits_but_delivers_final_value():
    b = Broadcaster()
    cond, q = b.subscribe()
    feed = ParamFeed(b, interval=0.05)
    feed.start()
    try:
        t0 = time.time()
        while time.time() - t0 < 0.3:  # a fast knob sweep: ~600 pushes
            feed.push("synth", "cutoff", time.time() - t0)
            time.sleep(0.0005)
        feed.push("synth", "cutoff", 42.0)
        time.sleep(0.2)
    finally:
        feed.stop()
    batches = [m for kind, m in q if kind == "params"]
    assert 2 <= len(batches) <= 12, len(batches)  # ~20 Hz, not hundreds
    assert batches[-1][-1]["value"] == 42.0  # the knob's final position always lands


def test_reconciler_notifies_cc_and_spec_changes_with_display_values(tmp_path):
    h = Rig(tmp_path)
    got = []
    h.r.param_listeners.append(lambda *a: got.append(a))
    h.put(DEVICE + "---" + BASIC)  # gain 6, mode Lead
    h.sync()
    assert got == []  # launch values come from -c, not live sets
    h.r.apply_cc("amp", "mode", 0.0)
    h.put(BASIC.replace("gain: 6", "gain: -3"))
    h.sync()
    assert got == [("amp", "mode", 0.0), ("amp", "gain", -3.0)]
    show = display_value(h.r)
    assert (show("amp", "mode", 0.0), show("amp", "bypass", 1.0), show("amp", "gain", 1.23456)) == ("Clean", True, 1.2346)
    assert show("nope", "x", 0.123456) == 0.1235
