import os
import stat
import time

from r7s.backend.base import InstanceSpec
from r7s.backend.jalv import JalvInstance


def test_jalv_is_never_asked_for_a_ui(tmp_path):
    argv = tmp_path / "argv"
    fake = tmp_path / "jalv"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv}\nread line\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    inst = JalvInstance(str(fake), InstanceSpec("amp", "urn:x", ui=True), {"gain": 1.5}, dict(os.environ))
    for _ in range(100):
        if argv.exists() and argv.read_text():
            break
        time.sleep(0.02)
    inst.stop(timeout=1)
    args = argv.read_text().split()
    assert "-s" not in args
    assert args[:4] == ["-n", "r7s-amp", "-x", "-c"] and "gain=1.5" in args and args[-1] == "urn:x"



def _pwd_jalv(tmp_path):
    """A fake jalv that records its cwd, makes a temp dir like jalv does, and
    (like real jalv) leaves it behind on exit."""
    fake = tmp_path / "jalv"
    # jalv's own pattern is "jalv" + 6 chars (zix_create_temporary_directory "jalvXXXXXX")
    fake.write_text('#!/bin/sh\npwd > .cwd\nmkdir "$(printf jalv%06d $(($$ % 1000000)))"\nread line\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def _wait_for(path):
    for _ in range(150):
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.02)


def _jalv_dirs(d):
    return sorted(p.name for p in d.iterdir() if p.name.startswith("jalv"))


def test_backend_init_clears_only_empty_leftovers(tmp_path):
    from r7s.backend.jalv import JalvBackend

    work = tmp_path / "work"
    (work / "plugins" / "drums").mkdir(parents=True)
    (work / "jalvAB12cd").mkdir()                          # old layout leftover: removed
    (work / "plugins" / "drums" / "jalvXY34ef").mkdir()    # per-plugin leftover: removed
    (work / "jalvKEEP01").mkdir()
    (work / "jalvKEEP01" / "state.ttl").write_text("x")    # not empty: kept
    (work / "jalvtoolong1").mkdir()                         # not jalv's 6-char pattern: kept
    (work / "notes").mkdir()                                # unrelated: kept
    target = tmp_path / "elsewhere"
    target.mkdir()
    (work / "jalvLINK01").symlink_to(target)                # symlink: kept, target untouched
    JalvBackend([], jalv_path="/bin/true", work_dir=work)
    assert sorted(p.name for p in work.iterdir()) == ["jalvKEEP01", "jalvLINK01", "jalvtoolong1", "notes", "plugins"]
    assert list((work / "plugins" / "drums").iterdir()) == [] and target.is_dir()


def test_each_plugin_runs_in_its_own_dir_and_stop_cleans_only_that(tmp_path):
    from r7s.backend.jalv import JalvBackend

    work = tmp_path / "work"
    b = JalvBackend([], jalv_path=_pwd_jalv(tmp_path), work_dir=work)
    drums = b.start(InstanceSpec("drums", "urn:x"), {})
    synth = b.start(InstanceSpec("synth", "urn:y"), {})
    ddir, sdir = work / "plugins" / "drums", work / "plugins" / "synth"
    _wait_for(ddir / ".cwd")
    _wait_for(sdir / ".cwd")
    assert (ddir / ".cwd").read_text().strip() == str(ddir) and (sdir / ".cwd").read_text().strip() == str(sdir)
    for _ in range(100):
        if _jalv_dirs(ddir) and _jalv_dirs(sdir):
            break
        time.sleep(0.02)
    assert _jalv_dirs(ddir) and _jalv_dirs(sdir)   # each running instance has its temp dir
    drums.stop(timeout=1)
    assert _jalv_dirs(ddir) == []                  # drums' leftover cleared on stop
    assert _jalv_dirs(sdir)                         # synth is still running: its dir untouched
    synth.stop(timeout=1)
    assert _jalv_dirs(sdir) == []


def test_start_clears_a_crashed_predecessors_leftovers(tmp_path):
    from r7s.backend.jalv import JalvBackend

    work = tmp_path / "work"
    b = JalvBackend([], jalv_path=_pwd_jalv(tmp_path), work_dir=work)
    d = work / "plugins" / "drums"
    d.mkdir(parents=True)
    (d / "jalvDEAD01").mkdir()                      # left by an instance that crashed (no stop())
    inst = b.start(InstanceSpec("drums", "urn:x"), {})
    assert not (d / "jalvDEAD01").exists()
    inst.stop(timeout=1)
