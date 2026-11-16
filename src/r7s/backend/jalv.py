"""jalv backend: one console `jalv` process per plugin, driven over stdin.

Protocol (console frontend): `-c SYM=VAL` at launch, `set SYM VAL` live,
`quit` to stop. jalv must have working console `set` (>= 1.8.2, or the
patched build from third_party/jalv). r7s never closes a jalv's stdin on
purpose: distro 1.8.0 spins at 100% CPU on EOF.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .base import BackendError, InstanceSpec

log = logging.getLogger(__name__)

PATCHED_PREFIX = Path("~/.local/opt/r7s-jalv").expanduser()
FIXED_VERSION = (1, 8, 2)


def find_jalv(configured: str | None = None) -> str | None:
    for cand in (
        configured,
        os.environ.get("R7S_JALV"),
        str(PATCHED_PREFIX / "bin" / "jalv"),
        shutil.which("jalv"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def jalv_version(path: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([path, "-V"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"jalv (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def is_patched(path: str) -> bool:
    return (Path(path).resolve().parent.parent / "share" / "r7s-jalv" / "PATCHED").is_file()


JALV_TEMP_RE = re.compile(r"^jalv[A-Za-z0-9]{6}$")


def clear_empty_temp_dirs(work_dir: Path) -> list[Path]:
    """Remove jalv temp dirs left by killed instances: only in r7s's own work
    dir, only exact jalvXXXXXX names, only real (non-symlink) directories,
    and only if empty (rmdir refuses anything else)."""
    removed = []
    for entry in work_dir.iterdir():
        if not JALV_TEMP_RE.match(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            entry.rmdir()
            removed.append(entry)
        except OSError:
            pass  # not empty: leave it
    return removed


def _fmt(v: float) -> str:
    return f"{v:.9g}"


class JalvInstance:
    def __init__(self, exe: str, spec: InstanceSpec, params: dict[str, float], env: dict[str, str],
                 cwd: str | None = None):
        self.spec = spec
        self.cwd = Path(cwd) if cwd else None
        args = [exe, "-n", spec.client, "-x"]
        for sym, val in params.items():
            args += ["-c", f"{sym}={_fmt(val)}"]
        # Never "-s": with a native UI shown, console jalv stops reading
        # stdin (no live parameters, CC or quit) and segfaults on SIGTERM.
        # Native windows are deferred to the Carla backend (docs/DESIGN.md).
        # A file-backed preset is given as a *path*: jalv then reads the whole
        # file. Given the URI, jalv 1.8 builds the state without loading the
        # preset's data file and restores nothing (third_party/jalv/UPSTREAM.md).
        args.append(spec.preset_path or spec.uri)
        if (setpriv := shutil.which("setpriv")) is not None:
            # Die with r7sd even if it is SIGKILLed (stdin EOF also does it
            # for the patched jalv; this covers other builds).
            args = [setpriv, "--pdeathsig", "KILL", "--", *args]
        self._lock = threading.Lock()
        self._tail: collections.deque[str] = collections.deque(maxlen=200)
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=cwd,
            )
        except OSError as e:
            raise BackendError(f"cannot start jalv: {e}") from e
        self._reader = threading.Thread(target=self._read, name=f"jalv-out-{spec.id}", daemon=True)
        self._reader.start()
        if spec.preset_uri:
            # Shared-file (#fragment) preset: the console command loads its
            # data properly. It also resets controls, so re-send ours after.
            self._send(f"preset {spec.preset_uri}")
            for sym, val in params.items():
                self._send(f"set {sym} {_fmt(val)}")
        log.info("started %s (pid %s): %s", spec.client, self._proc.pid, spec.uri)

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            # The console prints a "> " prompt before each command's output.
            while line.startswith("> "):
                line = line[2:]
            if line.strip() and line.strip() != ">":
                self._tail.append(line)

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    def alive(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> int | None:
        return self._proc.poll()

    def _send(self, line: str) -> None:
        with self._lock:
            if self._proc.stdin is None or not self.alive():
                raise BackendError(f"{self.spec.client} is not running")
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                raise BackendError(f"{self.spec.client}: {e}") from e

    def set_param(self, symbol: str, value: float) -> None:
        self._send(f"set {symbol} {_fmt(value)}")

    def stop(self, timeout: float = 2.0) -> None:
        if self.alive():
            try:
                self._send("quit")
                self._proc.wait(timeout=timeout)
            except (BackendError, subprocess.TimeoutExpired):
                pass
        if self.alive():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        with self._lock:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
        log.info("stopped %s (rc %s)", self.spec.client, self._proc.returncode)
        # jalv leaves its temp dir behind even on a clean quit. This instance
        # has exited and nothing else runs in its directory, so clear it.
        if self.cwd is not None and self.cwd.is_dir():
            clear_empty_temp_dirs(self.cwd)

    def output_tail(self, n: int = 20) -> list[str]:
        return list(self._tail)[-n:]


class JalvBackend:
    name = "jalv"

    def __init__(self, plugin_paths: list[str], jalv_path: str | None = None, work_dir: Path | None = None):
        self.exe = find_jalv(jalv_path)
        # jalv makes a jalvXXXXXX temp dir in its working directory and leaves
        # it behind (even on a clean quit). Each plugin runs in its own
        # <work_dir>/plugins/<id>, cleared whenever that plugin starts or
        # stops; at backend start, clear leftovers from earlier daemons too.
        self.work_dir = work_dir
        if work_dir is not None:
            work_dir.mkdir(parents=True, exist_ok=True)
            clear_empty_temp_dirs(work_dir)  # pre-per-plugin layout
            plugins = work_dir / "plugins"
            if plugins.is_dir():
                for d in plugins.iterdir():
                    if d.is_dir() and not d.is_symlink():
                        clear_empty_temp_dirs(d)
        self.plugin_paths = [os.path.expanduser(p) for p in plugin_paths]
        self.version = jalv_version(self.exe) if self.exe else None
        self.patched = bool(self.exe and is_patched(self.exe))

    def problems(self) -> list[str]:
        if self.exe is None:
            return ["jalv not found; install it or build third_party/jalv/build.sh"]
        if not self.patched and (self.version is None or self.version < FIXED_VERSION):
            v = ".".join(map(str, self.version)) if self.version else "unknown"
            return [
                f"{self.exe} is jalv {v}: live parameter changes are ignored before 1.8.2. "
                "Build the patched jalv with third_party/jalv/build.sh"
            ]
        return []

    def info(self) -> dict:
        return {
            "backend": self.name,
            "path": self.exe,
            "version": ".".join(map(str, self.version)) if self.version else None,
            "patched": self.patched,
        }

    def start(self, spec: InstanceSpec, params: dict[str, float]) -> JalvInstance:
        if self.exe is None:
            raise BackendError("jalv not found")
        env = dict(os.environ)
        paths = list(self.plugin_paths)
        if spec.bundle:
            paths.insert(0, str(Path(spec.bundle).parent))
        env["LV2_PATH"] = ":".join(paths)
        cwd = None
        if self.work_dir is not None:
            d = self.work_dir / "plugins" / spec.id
            d.mkdir(parents=True, exist_ok=True)
            # Only one instance per plugin id runs at a time (the reconciler
            # stops the old one first), so anything here is from a previous
            # instance, e.g. one that crashed and never reached stop().
            clear_empty_temp_dirs(d)
            cwd = str(d)
        return JalvInstance(self.exe, spec, params, env, cwd)
