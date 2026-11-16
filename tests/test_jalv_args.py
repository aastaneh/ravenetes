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


def test_jalv_runs_in_the_backend_work_dir_and_only_empty_leftovers_are_cleared(tmp_path):
    from r7s.backend.jalv import JalvBackend

    work = tmp_path / "work"
    work.mkdir()
    (work / "jalvAB12cd").mkdir()                        # empty leftover: removed
    (work / "jalvKEEP01").mkdir()
    (work / "jalvKEEP01" / "state.ttl").write_text("x")  # not empty: kept
    (work / "jalvtoolong1").mkdir()                       # not jalv's 6-char pattern: kept
    (work / "notes").mkdir()                              # unrelated: kept
    target = tmp_path / "elsewhere"
    target.mkdir()
    (work / "jalvLINK01").symlink_to(target)              # symlink: kept, target untouched
    cwd_file = tmp_path / "cwd"
    fake = tmp_path / "jalv"
    fake.write_text(f"#!/bin/sh\npwd > {cwd_file}\nread line\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    b = JalvBackend([], jalv_path=str(fake), work_dir=work)
    assert sorted(p.name for p in work.iterdir()) == ["jalvKEEP01", "jalvLINK01", "jalvtoolong1", "notes"]
    assert target.is_dir()
    inst = b.start(InstanceSpec("x", "urn:x"), {})
    for _ in range(100):
        if cwd_file.exists() and cwd_file.read_text():
            break
        time.sleep(0.02)
    inst.stop(timeout=1)
    assert cwd_file.read_text().strip() == str(work)
