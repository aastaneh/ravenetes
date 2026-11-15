"""r7sctl logs and r7sctl system-check."""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from .client import Client, DaemonUnavailable
from .eventlog import PRIORITY_NAMES, JournalSink, Record, default_event_file, parse_priority
from .eventlog_read import Filters, LogReadError, follow_file, parse_time, read_file, read_journal, tail
from .store import default_state_dir


def _err(msg: str) -> None:
    click.echo(f"r7sctl: {msg}", err=True)


# --- logs -----------------------------------------------------------------------

PRIO_STYLE = {0: "red", 1: "red", 2: "red", 3: "red", 4: "yellow", 5: "cyan", 6: None, 7: "bright_black"}


def _format(r: Record, color: bool) -> str:
    ts = time.strftime("%b %d %H:%M:%S", time.localtime(r.time))
    prio = PRIORITY_NAMES.get(r.priority, str(r.priority))
    line = f"{ts}  {prio:<7}  {r.object:<28}  {r.reason}: {r.message}"
    style = PRIO_STYLE.get(r.priority)
    return click.style(line, fg=style) if color and style else line


def _detect_backend(socket: str | None, backend: str, file: str | None) -> tuple[str, Path | None]:
    if file:
        return "file", Path(file)
    if backend in ("journal", "file"):
        return backend, default_event_file(default_state_dir()) if backend == "file" else None
    try:
        code, st = Client(socket, timeout=2).get("/v1/status")
        logcfg = (st.get("daemon") or {}).get("log") or {}
        if code == 200 and logcfg.get("backend") == "file" and logcfg.get("file"):
            return "file", Path(logcfg["file"])
        if code == 200 and logcfg.get("backend") == "journal":
            return "journal", None
    except (DaemonUnavailable, OSError, ValueError):
        pass
    if shutil.which("journalctl") and JournalSink.available():
        return "journal", None
    return "file", default_event_file(default_state_dir())


def _register_logs(main: click.Group) -> None:
    @main.command()
    @click.option("-f", "--follow", is_flag=True, help="Keep printing new events.")
    @click.option("--since", help="e.g. '5 minutes ago', 'today', '14:30', '2026-11-15 14:30'.")
    @click.option("--until", "until_", help="Same formats as --since.")
    @click.option("-u", "--unit", help="plugin, plugin/amp, connection, device/scarlett, rig/NAME, r7sd, jack …")
    @click.option("-p", "--priority", help="Show this priority and more severe: err, warning, notice, info, debug.")
    @click.option("-t", "--type", "type_",
                  type=click.Choice(["xrun", "plugin", "connection", "device", "midi", "reconciler", "system"]))
    @click.option("--rig", help="Only events recorded while this rig was active.")
    @click.option("-n", "--lines", type=int, help="Show the last N events (default 50 without --since).")
    @click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
    @click.option("--backend", type=click.Choice(["auto", "journal", "file"]), default="auto", show_default=True)
    @click.option("--file", "file_", metavar="PATH", help="Read this events.jsonl instead.")
    @click.option("--socket", envvar="R7S_SOCKET", metavar="PATH")
    def logs(follow, since, until_, unit, priority, type_, rig, lines, output, backend, file_, socket):
        """Show r7sd's structured event log (journal or JSON file)."""
        try:
            f = Filters(
                since=parse_time(since) if since else None,
                until=parse_time(until_) if until_ else None,
                priority=parse_priority(priority) if priority else None,
                unit=unit, type=type_, rig=rig,
            )
        except ValueError as e:
            _err(str(e))
            sys.exit(2)
        if lines is None and since is None:
            lines = 50
        kind, path = _detect_backend(socket, backend, file_)
        color = output == "text" and sys.stdout.isatty()

        def show(r: Record) -> None:
            if output == "json":
                click.echo(r.to_json_line())
            else:
                click.echo(_format(r, color))
            sys.stdout.flush()

        try:
            if kind == "journal":
                # journalctl applies -n before our Python-side filters, so
                # over-fetch when filters it can't express are in play.
                fetch = None if (lines and (unit and "/" not in unit)) else lines
                recs = [] if lines == 0 else list(read_journal(f, since, until_, fetch, follow=False))
                for r in tail(recs, lines):
                    show(r)
                if follow:
                    for r in read_journal(f, None, None, 0, follow=True):
                        show(r)
            else:
                assert path is not None
                if not path.exists() and not follow:
                    _err(f"no event log at {path} (is r7sd using the journal? try --backend journal)")
                    sys.exit(1)
                for r in read_file(path, f, lines):
                    show(r)
                if follow:
                    for r in follow_file(path, f):
                        show(r)
        except LogReadError as e:
            _err(str(e))
            sys.exit(1)
        except KeyboardInterrupt:
            pass


# --- system-check -------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    hint: str | None = None


def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def check_pipewire() -> list[Check]:
    out = []
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    sock = os.path.join(runtime, "pipewire-0") if runtime else ""
    if not sock or not os.path.exists(sock):
        out.append(Check("PipeWire", "fail", "no PipeWire socket in $XDG_RUNTIME_DIR",
                         "systemctl --user start pipewire wireplumber (or use jackd: device edges won't resolve)"))
    else:
        rc, txt = _run(["pipewire", "--version"]) if shutil.which("pipewire") else (1, "")
        ver = next((ln.split()[-1] for ln in txt.splitlines() if "libpipewire" in ln.lower()), "")
        out.append(Check("PipeWire", "ok", f"running{f' (libpipewire {ver})' if ver else ''}"))
    if shutil.which("pw-jack"):
        out.append(Check("pipewire-jack", "ok", "pw-jack available"))
    else:
        out.append(Check("pipewire-jack", "warn", "pw-jack not found; r7sd will use the system libjack",
                         "sudo apt install pipewire-jack"))
    if shutil.which("pw-dump"):
        rc, _ = _run(["pw-dump"], 5)
        out.append(Check("pw-dump", "ok" if rc == 0 else "fail", "hardware inventory works" if rc == 0
                         else "pw-dump failed", None if rc == 0 else "is PipeWire running for this user?"))
    else:
        out.append(Check("pw-dump", "fail", "not found; Devices can't be resolved", "install pipewire-bin"))
    return out


def check_jack() -> Check:
    code = ("import jack; c = jack.Client('r7s-system-check', no_start_server=True); "
            "print(c.samplerate, c.blocksize, round(c.cpu_load(), 1)); c.close()")
    args = [sys.executable, "-c", code]
    if shutil.which("pw-jack") and "pipewire-0.3/jack" not in os.environ.get("LD_LIBRARY_PATH", ""):
        args = [shutil.which("pw-jack"), *args]
    rc, txt = _run(args, 10)
    last = txt.splitlines()[-1] if txt else ""
    parts = last.split()
    if rc == 0 and len(parts) == 3:
        rate, block, load = parts
        latency = int(block) / int(rate) * 1000
        return Check("JACK", "ok", f"{rate} Hz, {block} frames ({latency:.1f} ms), DSP load {load}%")
    return Check("JACK", "fail", f"can't connect a JACK client: {last or rc}",
                 "start PipeWire (with pipewire-jack) or jackd")


def check_jalv(configured: str | None) -> Check:
    from .backend.jalv import FIXED_VERSION, find_jalv, is_patched, jalv_version  # noqa: PLC0415

    exe = find_jalv(configured)
    if exe is None:
        return Check("jalv", "fail", "not found", "build the patched jalv: third_party/jalv/build.sh")
    v = jalv_version(exe)
    vs = ".".join(map(str, v)) if v else "unknown version"
    if is_patched(exe):
        return Check("jalv", "ok", f"{vs} (patched) at {exe}")
    if v and v >= FIXED_VERSION:
        return Check("jalv", "ok", f"{vs} at {exe}")
    return Check("jalv", "fail", f"{vs} at {exe}: ignores live parameter changes",
                 "build the patched jalv: third_party/jalv/build.sh")


def check_plugins() -> Check:
    from .config import resolve_plugin_paths  # noqa: PLC0415
    from .lv2.world import PluginWorld  # noqa: PLC0415

    paths = resolve_plugin_paths(())
    existing = [p for p in paths.paths if Path(os.path.expanduser(p)).is_dir()]
    world = PluginWorld.load(paths.paths)
    n = len(world.plugins)
    detail = f"{n} LV2 plugins in {len(existing)} of {len(paths.paths)} paths ({paths.origin})"
    if n == 0:
        return Check("plugins", "warn", detail, "install LV2 plugins (e.g. apt install lsp-plugins-lv2 zam-plugins)")
    return Check("plugins", "ok", detail)


def check_midi() -> list[Check]:
    from .hw.alsa import list_cards  # noqa: PLC0415
    from .hw.midi import MidiUnavailable, list_seq_ports  # noqa: PLC0415

    out = []
    try:
        ports = list_seq_ports()
        on_cards = [p for p in ports if p.card is not None]
        out.append(Check("ALSA sequencer", "ok", f"{len(ports)} ports, {len(on_cards)} on sound cards"))
    except MidiUnavailable as e:
        out.append(Check("ALSA sequencer", "warn", f"{e}; MIDI CC mapping is disabled",
                         "check /dev/snd/seq permissions (audio group) and that snd-seq is loaded"))
    try:
        cards = list_cards()
        out.append(Check("sound cards", "ok" if cards else "warn",
                         ", ".join(f"{c.number}:{c.id}" for c in cards) or "none found in /sys/class/sound"))
    except OSError as e:
        out.append(Check("sound cards", "warn", f"can't read sysfs: {e}"))
    return out


def check_realtime() -> list[Check]:
    out = []
    soft, hard = resource.getrlimit(resource.RLIMIT_RTPRIO)
    rtkit = _run(["pgrep", "-x", "rtkit-daemon"])[0] == 0
    if soft > 0 or rtkit:
        how = [f"RLIMIT_RTPRIO={soft}"] if soft > 0 else []
        if rtkit:
            how.append("rtkit-daemon running")
        out.append(Check("realtime", "ok", ", ".join(how)))
    else:
        out.append(Check("realtime", "warn", "no realtime priority available (RLIMIT_RTPRIO=0, no rtkit)",
                         "install rtkit, or add an @audio rtprio limit in /etc/security/limits.d/ "
                         "and join the audio group"))
    govs = sorted({_read(p) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")}
                  - {None})
    if govs:
        status = "ok" if govs == ["performance"] else "info"
        out.append(Check("CPU governor", status, ", ".join(govs),
                         None if status == "ok" else "'performance' avoids xruns from frequency scaling"))
    return out


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def check_logging(socket: str | None) -> Check:
    """What the running daemon writes to; otherwise what it would choose."""
    try:
        code, st = Client(socket, timeout=2).get("/v1/status")
        lg = (st.get("daemon") or {}).get("log") or {}
        if code == 200 and lg.get("backend") == "file":
            return Check("event log", "ok", f"JSON file at {lg.get('file')} (r7sd's Config)")
        if code == 200 and lg.get("backend") == "journal":
            return Check("event log", "ok", "systemd journal (r7sctl logs reads it)")
    except (DaemonUnavailable, OSError, ValueError):
        pass
    if JournalSink.available():
        return Check("event log", "ok", "systemd journal (r7sctl logs reads it)")
    return Check("event log", "ok", f"JSON file fallback at {default_event_file(default_state_dir())}")


def check_daemon(socket: str | None) -> list[Check]:
    out = []
    try:
        code, st = Client(socket, timeout=3).get("/v1/status")
        d = st.get("daemon", {})
        rig = st.get("activeRig")
        phase = (st.get("rigs", {}).get(rig) or {}).get("phase") if rig else None
        out.append(Check("r7sd", "ok", f"{d.get('version')} running (pid {d.get('pid')}); "
                         + (f"Rig/{rig} {phase}" if rig else "no active rig")
                         + f"; {st.get('xruns', 0)} xruns"))
    except DaemonUnavailable:
        out.append(Check("r7sd", "warn", "not running", "systemctl --user start r7sd, or run r7sd"))
    unit = Path("~/.config/systemd/user/r7sd.service").expanduser()
    if shutil.which("systemctl"):
        rc, txt = _run(["systemctl", "--user", "is-enabled", "r7sd"])
        if rc == 0:
            out.append(Check("systemd unit", "ok", f"r7sd.service {txt}"))
        elif unit.exists():
            out.append(Check("systemd unit", "info", "installed but not enabled",
                             "systemctl --user enable --now r7sd"))
        else:
            out.append(Check("systemd unit", "info", "not installed",
                             "cp packaging/systemd/r7sd.service ~/.config/systemd/user/"))
    return out


SYMBOL = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}
COLOR = {"ok": "green", "warn": "yellow", "fail": "red", "info": None}


def _register_check(main: click.Group) -> None:
    @main.command("system-check")
    @click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
    @click.option("--socket", envvar="R7S_SOCKET", metavar="PATH")
    def system_check(output, socket):
        """Check everything r7s needs: PipeWire/JACK, jalv, plugins, MIDI, realtime."""
        checks: list[Check] = []
        checks += check_pipewire()
        checks.append(check_jack())
        checks.append(check_jalv(None))
        checks.append(check_plugins())
        checks += check_midi()
        checks += check_realtime()
        checks.append(check_logging(socket))
        checks += check_daemon(socket)
        failed = sum(c.status == "fail" for c in checks)
        if output == "json":
            click.echo(json.dumps({"ok": failed == 0, "checks": [asdict(c) for c in checks]}, indent=2))
        else:
            tty = sys.stdout.isatty()
            w = max(len(c.name) for c in checks)
            for c in checks:
                sym = SYMBOL[c.status]
                if tty and COLOR[c.status]:
                    sym = click.style(sym, fg=COLOR[c.status], bold=True)
                click.echo(f"{sym} {c.name:<{w}}  {c.detail}")
                if c.hint and c.status != "ok":
                    click.echo(f"  {'':<{w}}  → {c.hint}")
            warns = sum(c.status == "warn" for c in checks)
            click.echo(f"\n{failed} problem(s), {warns} warning(s)")
        sys.exit(1 if failed else 0)


def register(main: click.Group) -> None:
    _register_logs(main)
    _register_check(main)


