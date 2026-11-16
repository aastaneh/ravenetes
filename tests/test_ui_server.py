import http.client
import json
import threading
import time

import pytest

from r7s.ui.server import UiServer

from .test_reconciler import DEVICE, Rig
from .test_ui_model import RIG


@pytest.fixture
def ui(tmp_path):
    h = Rig(tmp_path)
    h.put(DEVICE + "---" + RIG)
    h.sync()
    srv = UiServer(h.r, h.store, lambda: {"ok": True})
    msg = srv.ensure(True, "127.0.0.1:0")
    assert msg and msg.startswith("web UI at http://localhost:")
    yield h, srv
    srv.stop()


def get(srv, path, host=None, method="GET"):
    port = srv._server.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers={"Host": host or f"localhost:{port}"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_static_and_api(ui):
    h, srv = ui
    code, hdrs, body = get(srv, "/")
    assert code == 200 and b"<title>Ravenetes</title>" in body
    assert "default-src 'self'" in hdrs["Content-Security-Policy"] and hdrs["X-Content-Type-Options"] == "nosniff"
    for path, ctype in (("/app.js", "text/javascript"), ("/layout.js", "text/javascript"), ("/app.css", "text/css")):
        code, hdrs, _ = get(srv, path)
        assert code == 200 and hdrs["Content-Type"].startswith(ctype)
    code, _, body = get(srv, "/api/graph")
    g = json.loads(body)
    assert code == 200 and g["rig"] == "r" and len(g["nodes"]) == 4
    assert json.loads(get(srv, "/api/status")[2]) == {"ok": True}
    assert get(srv, "/nope")[0] == 404


def test_read_only_and_host_check(ui):
    h, srv = ui
    assert get(srv, "/api/graph", method="POST")[0] == 405
    assert get(srv, "/api/graph", method="DELETE")[0] == 405
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1
    assert get(srv, "/api/graph", host="evil.example:7377")[0] == 421
    assert get(srv, "/api/graph", host="127.0.0.1")[0] == 200


def test_sse_stream_delivers_events(ui):
    h, srv = ui
    port = srv._server.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/stream", headers={"Host": "localhost"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    for _ in range(50):
        if srv.broadcaster.clients:
            break
        time.sleep(0.02)
    threading.Timer(0.1, lambda: srv.broadcaster.publish("event", {"reason": "Crashed"})).start()
    buf = b""
    while b"Crashed" not in buf:
        buf += r.fp.readline()
    assert b"event: event\n" in buf and b'"reason": "Crashed"' in buf
    c.close()


def test_listen_changes_and_bind_failure(ui):
    h, srv = ui
    old = srv.listen
    assert srv.ensure(True, old) is None  # unchanged: no restart
    assert srv.ensure(False, old) == "web UI disabled" and srv.url is None
    other = UiServer(h.r, h.store, lambda: {})
    assert other.ensure(True, "127.0.0.1:0")
    taken = f"127.0.0.1:{other._server.server_address[1]}"
    assert srv.ensure(True, taken) is None and "can't listen" in srv.error
    assert srv.ensure(True, taken) is None  # no retry storm on the same bad config
    other.stop()


def test_hidden_attribute_always_hides():
    # Regression: .empty { display: grid } overrode [hidden], leaving an
    # invisible overlay over the graph that ate every click.
    from importlib import resources
    css = resources.files("r7s.ui").joinpath("static", "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css
