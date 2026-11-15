import stat

from r7s.cli_system import check_jalv


def fake_jalv(dirpath, version, patched=False):
    bindir = dirpath / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "jalv"
    exe.write_text(f"#!/bin/sh\necho 'jalv {version} <http://drobilla.net/software/jalv>'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    if patched:
        (dirpath / "share" / "r7s-jalv").mkdir(parents=True)
        (dirpath / "share" / "r7s-jalv" / "PATCHED").write_text("0001\n")
    return str(exe)


def test_check_jalv_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    distro = fake_jalv(tmp_path / "distro", "1.8.0")
    c = check_jalv(distro)
    assert c.status == "fail" and "ignores live parameter changes" in c.detail and "build.sh" in c.hint
    assert check_jalv(fake_jalv(tmp_path / "patched", "1.8.0", patched=True)).status == "ok"
    assert check_jalv(fake_jalv(tmp_path / "new", "1.10.0")).status == "ok"
    monkeypatch.setattr("r7s.backend.jalv.PATCHED_PREFIX", tmp_path / "none")
    monkeypatch.delenv("R7S_JALV", raising=False)
    assert check_jalv(None).status == "fail"
