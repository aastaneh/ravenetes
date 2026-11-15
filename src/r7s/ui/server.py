"""Read-only web UI: static page + JSON + Server-Sent Events, over TCP.

Never exposes apply/delete. Bound to localhost by default (Config
spec.ui.listen); on loopback the Host header is checked so a hostile page
can't use DNS rebinding to read the rig through the user's browser.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .model import build_graph

log = logging.getLogger(__name__)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/layout.js": ("layout.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _static(name: str) -> bytes:
    return resources.files("r7s.ui").joinpath("static", name).read_bytes()


class Broadcaster:
    """Fan-out of small messages to SSE clients; slow clients drop old ones."""

    def __init__(self, depth: int = 200):
        self._lock = threading.Lock()
        self._clients: list[tuple[threading.Condition, collections.deque]] = []
        self._depth = depth

    def subscribe(self):
        cond = threading.Condition()
        q: collections.deque = collections.deque(maxlen=self._depth)
        with self._lock:
            self._clients.append((cond, q))
        return cond, q

    def unsubscribe(self, client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def publish(self, kind: str, payload) -> None:
        with self._lock:
            clients = list(self._clients)
        for cond, q in clients:
            with cond:
                q.append((kind, payload))
                cond.notify()

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._clients)


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host in ("localhost",)


def make_handler(reconciler, store, broadcaster: Broadcaster, bound_host: str, status_fn):
    allowed_hosts = None
    if _loopback(bound_host):
        allowed_hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "r7sd-ui"

        def log_message(self, fmt, *args) -> None:
            log.debug("ui: " + fmt, *args)

        def _host_ok(self) -> bool:
            if allowed_hosts is None:
                return True
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in allowed_hosts

        def _headers(self, code: int, ctype: str, length: int | None = None, cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()

        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self._headers(code, "application/json", len(body))
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(421, {"error": "unexpected Host header"})
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in STATIC:
                    name, ctype = STATIC[url.path]
                    body = _static(name)
                    self._headers(200, ctype, len(body), cache=False)
                    self.wfile.write(body)
                elif url.path == "/api/graph":
                    self._json(200, build_graph(reconciler, store, q.get("rig") or None))
                elif url.path == "/api/status":
                    self._json(200, status_fn())
                elif url.path == "/api/events":
                    since = float(q.get("since", 0))
                    self._json(200, {"events": [e.to_dict() for e in reconciler.events if e.time > since]})
                elif url.path == "/api/stream":
                    self._stream()
                else:
                    self._json(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log.exception("ui error")
                try:
                    self._json(500, {"error": str(e)})
                except OSError:
                    pass

        def _stream(self) -> None:
            self._headers(200, "text/event-stream")
            client = broadcaster.subscribe()
            cond, q = client
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    with cond:
                        if not q:
                            cond.wait(timeout=15)
                        items = list(q)
                        q.clear()
                    if not items:
                        self.wfile.write(b": ping\n\n")  # keepalive through proxies
                    for kind, payload in items:
                        data = json.dumps(payload, default=str)
                        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                broadcaster.unsubscribe(client)

        def _refuse(self) -> None:
            self._json(405, {"error": "the web UI is read-only"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class UiServer:
    """Starts, restarts and stops the listener as Config spec.ui changes."""

    def __init__(self, reconciler, store, status_fn, broadcaster: Broadcaster | None = None):
        self.reconciler = reconciler
        self.store = store
        self.status_fn = status_fn
        self.broadcaster = broadcaster or Broadcaster()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.listen: str | None = None
        self.error: str | None = None
        self._failed_for: str | None = None

    @property
    def url(self) -> str | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        shown = "localhost" if _loopback(str(host)) else ("<this-host>" if host in ("0.0.0.0", "::") else host)
        return f"http://{shown}:{port}/"

    def ensure(self, enabled: bool, listen: str) -> str | None:
        """Make the listener match the config. Returns an event message on change."""
        want = listen if enabled else None
        if want == self.listen and (self._server is not None or want is None):
            return None
        if want is not None and want == self._failed_for:
            return None  # don't retry a bind that failed until the config changes
        self.stop()
        self.listen = want
        if want is None:
            return "web UI disabled"
        host, _, port = want.rpartition(":")
        host = host.strip("[]")
        cls = _Server6 if ":" in host else _Server
        try:
            srv = cls((host, int(port)), make_handler(self.reconciler, self.store, self.broadcaster,
                                                     host, self.status_fn))
        except OSError as e:
            self.error = f"can't listen on {want}: {e}"
            self._failed_for = want
            self.listen = None
            return None
        self.error = None
        self._failed_for = None
        self._server = srv
        self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="ui", daemon=True)
        self._thread.start()
        return f"web UI at {self.url}" + ("" if _loopback(host) else " (exposed on the network)")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
