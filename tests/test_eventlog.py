import json
import socket
import struct
import threading
import time
from datetime import datetime

import pytest

from r7s.eventlog import EventLog, JournalSink, JsonFileSink, Record, journal_payload, parse_priority, priority_for
from r7s.eventlog_read import Filters, follow_file, journal_args, parse_time, read_file, record_from_journal

from .test_reconciler import DEVICE, Rig, rig_doc
from .test_reconciler import BASIC as BASIC_RIG


def parse_native(payload: bytes) -> dict:
    """Decode journald's native protocol (both KEY=value and binary forms)."""
    out, i = {}, 0
    while i < len(payload):
        nl = payload.index(b"\n", i)
        line = payload[i:nl]
        if b"=" in line:
            k, v = line.split(b"=", 1)
            out[k.decode()] = v.decode()
            i = nl + 1
        else:
            (n,) = struct.unpack("<Q", payload[nl + 1:nl + 9])
            out[line.decode()] = payload[nl + 9:nl + 9 + n].decode()
            i = nl + 9 + n + 1
    return out


def test_journal_payload_fields_and_multiline():
    r = Record(1.0, "plugin", "Crashed", "plugin/amp", "exited with -11\nsegfault", 3, "guitar",
               {"exit code": -11, "tail": "a\nb"})
    d = parse_native(journal_payload(r))
    assert d["SYSLOG_IDENTIFIER"] == "r7sd" and d["PRIORITY"] == "3"
    assert d["R7S_TYPE"] == "plugin" and d["R7S_OBJECT"] == "plugin/amp" and d["R7S_RIG"] == "guitar"
    assert d["R7S_TEXT"] == "exited with -11\nsegfault"
    assert d["R7S_EXIT_CODE"] == "-11" and d["R7S_TAIL"] == "a\nb"
    assert d["MESSAGE"].startswith("Crashed plugin/amp: exited")


def test_journal_sink_sends_datagrams(tmp_path):
    path = str(tmp_path / "journal.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    try:
        sink = JournalSink(path)
        sink.write(Record(time.time(), "xrun", "Xrun", "rig/r", "xrun", 4, "r", {"delay_usecs": 812}))
        d = parse_native(srv.recv(65536))
        assert d["R7S_DELAY_USECS"] == "812" and d["PRIORITY"] == "4"
        sink.close()
        bad = JournalSink(str(tmp_path / "missing.sock"))
        bad.write(Record(time.time(), "system", "x", "r7sd", "m"))
        assert bad.dropped == 1  # never raises
    finally:
        srv.close()


def test_priorities():
    assert priority_for("plugin", "Crashed") == 3
    assert priority_for("xrun", "Xrun") == 4
    assert priority_for("plugin", "Started") == 6 and priority_for("system", "Started") == 5
    assert priority_for("plugin", "ParameterSet") == 7
    assert (parse_priority("err"), parse_priority("4"), parse_priority(7)) == (3, 4, 7)
    with pytest.raises(ValueError):
        parse_priority("loud")


def test_eventlog_folds_duplicates_in_memory_but_writes_all(tmp_path):
    sink = JsonFileSink(tmp_path / "e.jsonl")
    log = EventLog([sink], rig=lambda: "r")
    for _ in range(3):
        log.emit("connection", "Restored", "connection/a->b", "x")
    log.emit("plugin", "Crashed", "plugin/amp", "boom")
    assert [(r.reason, r.count) for r in log.recent] == [("Restored", 3), ("Crashed", 1)]
    lines = (tmp_path / "e.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[-1])["rig"] == "r"


def test_file_rotation_and_reading_across_files(tmp_path):
    p = tmp_path / "events.jsonl"
    sink = JsonFileSink(p, max_bytes=600, backups=2)
    for i in range(20):
        sink.write(Record(1000.0 + i, "plugin", "ParameterSet", "plugin/amp", f"gain = {i}", 7))
    sink.close()
    assert p.exists() and (tmp_path / "events.jsonl.1").exists() and (tmp_path / "events.jsonl.2").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    recs = read_file(p, Filters())
    times = [r.time for r in recs]
    assert times == sorted(times) and times[-1] == 1019.0  # oldest rotated first, newest last
    assert [r.message for r in read_file(p, Filters(), lines=2)] == ["gain = 18", "gain = 19"]


def test_filters(tmp_path):
    recs = [
        Record(100, "plugin", "Crashed", "plugin/amp", "a", 3, "r1"),
        Record(200, "plugin", "Started", "plugin/verb", "b", 6, "r1"),
        Record(300, "connection", "Connected", "connection/amp:out->verb:in", "c", 6, "r2"),
        Record(400, "xrun", "Xrun", "rig/r2", "d", 4, "r2"),
    ]
    sel = lambda f: [r.message for r in recs if f.match(r)]  # noqa: E731
    assert sel(Filters(unit="plugin")) == ["a", "b"]
    assert sel(Filters(unit="plugin/amp")) == ["a"]
    assert sel(Filters(unit="connection")) == ["c"]
    assert sel(Filters(priority=4)) == ["a", "d"]
    assert sel(Filters(since=150, until=350)) == ["b", "c"]
    assert sel(Filters(rig="r2", type="xrun")) == ["d"]


def test_parse_time():
    now = datetime(2026, 11, 15, 15, 30, 0).timestamp()
    assert parse_time("5 minutes ago", now) == now - 300
    assert parse_time("-2h", now) == now - 7200
    assert parse_time("now", now) == now
    assert parse_time("today", now) == datetime(2026, 11, 15).timestamp()
    assert parse_time("yesterday", now) == datetime(2026, 11, 14).timestamp()
    assert parse_time("14:05", now) == datetime(2026, 11, 15, 14, 5).timestamp()
    assert parse_time("2026-11-01 08:00", now) == datetime(2026, 11, 1, 8, 0).timestamp()
    assert parse_time("@1794780000", now) == 1794780000.0
    with pytest.raises(ValueError):
        parse_time("whenever", now)


def test_follow_survives_rotation(tmp_path):
    p = tmp_path / "events.jsonl"
    sink = JsonFileSink(p, max_bytes=500, backups=1)
    sink.write(Record(1.0, "system", "Started", "r7sd", "before follow"))
    got = []

    def reader():
        for r in follow_file(p, Filters(), poll=0.02):
            got.append(r.message)
            if len(got) == 12:
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.15)
    for i in range(12):  # ~120 bytes each: rotates several times
        sink.write(Record(2.0 + i, "plugin", "ParameterSet", "plugin/amp", f"m{i}", 7))
        time.sleep(0.03)
    t.join(timeout=5)
    sink.close()
    assert got == [f"m{i}" for i in range(12)]


def test_journal_reader_mapping_and_args():
    e = {"__REALTIME_TIMESTAMP": "1794784488314980", "PRIORITY": "4", "R7S_TYPE": "xrun",
         "R7S_REASON": "Xrun", "R7S_OBJECT": "rig/r", "R7S_TEXT": "xrun (delay 812 us)",
         "R7S_RIG": "r", "R7S_DELAY_USECS": "812", "MESSAGE": "Xrun rig/r: xrun (delay 812 us)"}
    r = record_from_journal(e)
    assert (r.type, r.priority, r.rig, r.fields) == ("xrun", 4, "r", {"delay_usecs": "812"})
    assert abs(r.time - 1794784488.31498) < 1e-6
    assert record_from_journal({"MESSAGE": "plain stderr line"}) is None
    args = journal_args(Filters(type="plugin", unit="plugin/amp", priority=3), "5 minutes ago", None, 20, True)
    assert args[:2] == ["journalctl", "--user"]
    for a in ("SYSLOG_IDENTIFIER=r7sd", "R7S_TYPE=plugin", "R7S_OBJECT=plugin/amp", "-f"):
        assert a in args
    assert args[args.index("-p") + 1] == "err" and args[args.index("--since") + 1] == "5 minutes ago"
    # A bare kind can't be a journal match; it's filtered in Python instead.
    assert not any(a.startswith("R7S_OBJECT=") for a in journal_args(Filters(unit="plugin"), None, None, None, False))


# --- reconciler events ------------------------------------------------------------


@pytest.fixture
def h(tmp_path):
    return Rig(tmp_path)


def reasons(h):
    return [e.reason for e in h.r.events]


def test_xruns_single_and_burst(h):
    h.put(DEVICE + "---" + BASIC_RIG)
    h.sync()
    h.graph.load = 87.5
    h.graph.xruns = [(time.time(), 812.0)]
    h.sync(1)
    x = [e for e in h.r.events if e.type == "xrun"]
    assert len(x) == 1 and x[0].rig == "r" and x[0].fields["delay_usecs"] == 812
    assert x[0].fields["cpu_load"] == 87.5 and x[0].priority == 4
    h.graph.xruns = [(time.time(), d) for d in (100.0, 2500.0, 300.0, 50.0, 10.0)]
    h.sync(1)
    burst = [e for e in h.r.events if e.type == "xrun"][-1]
    assert burst.fields["count"] == 5 and burst.fields["delay_usecs"] == 2500
    st = h.r.status
    assert st["xruns"] == 6 and st["rigs"]["r"]["xruns"] == 6 and st["cpuLoad"] == 87.5


def test_device_rig_and_samplerate_events(h):
    h.put(DEVICE + "---" + BASIC_RIG)
    h.sync()
    assert "RigActivated" in reasons(h)
    h.plug_scarlett(False)
    h.sync()
    h.plug_scarlett(True)
    h.sync()
    dev = [(e.reason, e.object) for e in h.r.events if e.type == "device"]
    assert dev == [("DeviceDisconnected", "device/scarlett"), ("DeviceConnected", "device/scarlett")]
    h.graph.sample_rate = 44100
    h.sync(1)
    sr = [e for e in h.r.events if e.reason == "SampleRateChanged"]
    assert sr and sr[0].fields == {"old": 48000, "new": 44100}
    h.store.delete("ActiveRig", "current")
    h.sync()
    assert "RigDeactivated" in reasons(h)


def test_connection_objects_are_typed(h):
    h.put(DEVICE + "---" + BASIC_RIG)
    h.sync()
    conn = [e.object for e in h.r.events if e.type == "connection"]
    assert conn and all(o.startswith("connection/") for o in conn)


def test_cc_value_logging_is_opt_in(h):
    h.put(DEVICE + "---" + BASIC_RIG)
    h.sync()
    h.r.apply_cc("amp", "gain", 1.0)
    assert "CcValue" not in reasons(h)
    h.put("apiVersion: r7s/v1\nkind: Config\nspec: {log: {midiCC: true}}\n")
    h.sync(1)
    h.r.apply_cc("amp", "gain", 2.0)
    cc = [e for e in h.r.events if e.reason == "CcValue"]
    assert len(cc) == 1 and cc[0].fields == {"symbol": "gain", "value": 2.0} and cc[0].priority == 7


def test_log_backend_switch(h, tmp_path):
    from r7s.eventlog import choose_sink

    made = []

    def make_sink(backend):
        s = choose_sink(backend, tmp_path / "logs", journal_socket=str(tmp_path / "nojournal"))
        made.append(s)
        return s

    h.r._make_sink = make_sink
    h.put(DEVICE + "---" + rig_doc())
    h.sync(1)  # first pass adopts the daemon's choice without reopening
    assert made == []
    h.put("apiVersion: r7s/v1\nkind: Config\nspec: {log: {backend: file}}\n")
    h.sync(1)
    assert [s.name for s in made] == ["file"] and h.r.log.backend == "file"
    assert "LogBackendChanged" in reasons(h)
    assert (tmp_path / "logs" / "events.jsonl").exists()


def test_xrun_after_graph_change_is_annotated(h):
    h.put(DEVICE + "---" + BASIC_RIG)
    h.sync()  # starts a plugin and cables it: a graph change "now"
    h.graph.xruns = [(time.time(), 0.0)]
    h.sync(1)
    x = [e for e in h.r.events if e.type == "xrun"][-1]
    assert "during a graph change" in x.message and x.fields["graph_change"] is True
    h.clock.t += 5
    h.graph.xruns = [(time.time(), 900.0)]
    h.sync(1)
    x = [e for e in h.r.events if e.type == "xrun"][-1]
    assert "graph change" not in x.message and "graph_change" not in x.fields


def test_tail_semantics():
    from r7s.eventlog_read import tail
    assert (tail([1, 2, 3], None), tail([1, 2, 3], 2), tail([1, 2, 3], 0)) == ([1, 2, 3], [2, 3], [])
