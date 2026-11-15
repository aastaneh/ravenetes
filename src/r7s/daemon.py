"""r7sd: the reconciler daemon, serving the r7s API on a Unix socket."""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import shutil
import signal
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .api import Api, dumps
from .backend.jalv import JalvBackend
from .config import resolve_plugin_paths
from .hw import inventory
from .jackgraph import JackGraph
from .loader import LoadResult
from .eventlog import EventLog, choose_sink
from .midi_cc import CcRouter
from .lv2.world import PluginWorld
from .reconciler import Reconciler
from .store import Store, default_state_dir
from .validate import rig_bundles

log = logging.getLogger("r7sd")


def default_socket() -> str:
    if (env := os.environ.get("R7S_SOCKET")):
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "r7s.sock")
    return f"/tmp/r7s-{os.getuid()}.sock"


# --- HTTP over a Unix socket ------------------------------------------------


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"r7sd/{__version__}"

        def address_string(self) -> str:
            return "unix"

        def log_message(self, fmt, *args) -> None:
            log.debug("api: " + fmt, *args)

        def _send(self, code: int, payload) -> None:
            body = dumps(payload)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n))

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            try:
                if parts[:1] != ["v1"]:
                    return self._send(404, {"error": "not found"})
                parts = parts[1:]
                if method == "GET" and parts == ["status"]:
                    return self._send(*api.status())
                if method == "GET" and parts == ["events"]:
                    return self._send(*api.events(float(q.get("since", 0))))
                if method == "GET" and parts == ["resources"]:
                    return self._send(*api.list(q.get("kind")))
                if method == "GET" and len(parts) == 3 and parts[0] == "resources":
                    return self._send(*api.get(parts[1], parts[2]))
                if method == "POST" and parts == ["apply"]:
                    return self._send(*api.apply(self._body()))
                if method == "POST" and parts == ["diff"]:
                    return self._send(*api.diff(self._body()))
                if method == "POST" and parts == ["delete"]:
                    b = self._body()
                    return self._send(*api.delete(b.get("refs") or [], bool(b.get("dryRun"))))
                if method == "GET" and len(parts) == 2 and parts[0] == "snapshot":
                    return self._send(*api.snapshot(parts[1]))
                return self._send(404, {"error": f"no route for {method} {url.path}"})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": f"bad request: {e}"})
            except Exception as e:  # report, don't kill the server thread silently
                log.exception("API error")
                return self._send(500, {"error": str(e)})

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


def _claim_socket(path: str) -> None:
    """Remove a stale socket; refuse if another r7sd answers on it."""
    if not os.path.exists(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError as e:
        if e.errno in (errno.ECONNREFUSED, errno.ENOENT):
            os.unlink(path)
            return
        raise
    finally:
        s.close()
    raise SystemExit(f"r7sd: another r7sd is already listening on {path}")


# --- wiring -------------------------------------------------------------------


def _config(res: LoadResult):
    cs = res.of_kind("Config")
    return cs[0].resource.spec if cs else None


def make_world(res: LoadResult) -> PluginWorld:
    paths = resolve_plugin_paths((), res).paths
    return PluginWorld.load(paths, extra_bundles=rig_bundles(res))


def make_backend(res: LoadResult) -> JalvBackend:
    cfg = _config(res)
    return JalvBackend(resolve_plugin_paths((), res).paths, cfg.jalv_path if cfg else None)


def backend_key(res: LoadResult):
    cfg = _config(res)
    return (tuple(resolve_plugin_paths((), res).paths), cfg.jalv_path if cfg else None)


def _stderr_is_journal() -> bool:
    """systemd sets JOURNAL_STREAM=dev:ino when stderr goes to the journal."""
    js = os.environ.get("JOURNAL_STREAM", "")
    try:
        dev, ino = (int(x) for x in js.split(":"))
        st = os.fstat(2)
    except (ValueError, OSError):
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


def _stderr_echo(sink):
    """Echo events to stderr for terminals; skip it when stderr *is* the
    journal and events already go there with structured fields."""
    if sink.name == "journal" and _stderr_is_journal():
        return None
    elog = logging.getLogger("r7s.events")

    def echo(r) -> None:
        level = logging.WARNING if r.priority <= 4 else logging.DEBUG if r.priority == 7 else logging.INFO
        elog.log(level, "%s %s %s: %s", r.type, r.reason, r.object, r.message)

    return echo


def maybe_reexec_under_pw_jack(argv: list[str]) -> None:
    """Make sure libjack is PipeWire's when PipeWire is running."""
    if os.environ.get("R7S_NO_REEXEC") or "pipewire-0.3/jack" in os.environ.get("LD_LIBRARY_PATH", ""):
        return
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.exists(os.path.join(runtime, "pipewire-0")):
        return  # no PipeWire: use whatever libjack (jackd) is installed
    pw_jack = shutil.which("pw-jack")
    if pw_jack is None:
        return
    os.environ["R7S_NO_REEXEC"] = "1"
    os.execv(pw_jack, [pw_jack, sys.executable, "-m", "r7s.daemon", *argv])


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="r7sd", description="Ravenetes reconciler daemon")
    ap.add_argument("--socket", default=default_socket(), help="API socket path")
    ap.add_argument("--state-dir", type=Path, default=default_state_dir())
    ap.add_argument("--log-level", default=os.environ.get("R7S_LOG_LEVEL", "info"))
    ap.add_argument("--version", action="version", version=f"r7sd {__version__}")
    args = ap.parse_args(argv)
    maybe_reexec_under_pw_jack(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    store = Store(args.state_dir)
    for problem in store.load():
        log.warning("%s", problem)

    cfg = _config(store.as_load_result())
    make_sink = lambda backend: choose_sink(backend, args.state_dir)  # noqa: E731
    sink = make_sink(cfg.log.backend if cfg else "auto")
    events = EventLog([sink], echo=_stderr_echo(sink))
    log.info("event log: %s%s", sink.name, f" ({sink.path})" if sink.name == "file" else "")

    reconciler = Reconciler(
        store,
        make_backend=make_backend,
        make_graph=lambda cb: JackGraph("r7sd", on_change=cb),
        make_world=make_world,
        hw_snapshot=inventory.snapshot,
        backend_key=backend_key,
        make_cc_router=lambda sink: CcRouter(sink),
        eventlog=events,
        make_sink=make_sink,
    )
    api = Api(store, reconciler, make_world, inventory.snapshot, args.socket)

    _claim_socket(args.socket)
    old_umask = os.umask(0o177)  # socket is private to this user
    try:
        server = UnixHTTPServer(args.socket, make_handler(api))
    finally:
        os.umask(old_umask)

    stopping = threading.Event()

    def on_signal(signum, frame):
        if not stopping.is_set():
            stopping.set()
            log.info("received %s; shutting down", signal.Signals(signum).name)
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    reconciler.event("system", "Started", "r7sd", f"r7sd {__version__} listening on {args.socket}")
    reconciler.start()
    reconciler.poke()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        reconciler.event("system", "Stopping", "r7sd", "shutting down")
        reconciler.shutdown()
        server.server_close()
        reconciler.event("system", "Stopped", "r7sd", "stopped")
        events.close()
        try:
            os.unlink(args.socket)
        except FileNotFoundError:
            pass
        log.info("stopped")


if __name__ == "__main__":
    main()
