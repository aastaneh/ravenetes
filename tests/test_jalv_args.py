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
