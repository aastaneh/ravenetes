from __future__ import annotations

import io
import textwrap
from pathlib import Path

import pytest

from r7s.loader import LoadResult, load_text
from r7s.lv2.world import PluginWorld

FIXTURES = Path(__file__).parent / "fixtures"
LV2_DIR = FIXTURES / "lv2"


@pytest.fixture
def world() -> PluginWorld:
    return PluginWorld.load([LV2_DIR], cache_dir=False)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("LV2_PATH", raising=False)


def parse(text: str, source: str = "test.yaml", base_dir: Path | None = None) -> LoadResult:
    result = LoadResult()
    load_text(textwrap.dedent(text), source, base_dir or FIXTURES, result)
    return result


def stdin(text: str) -> io.StringIO:
    return io.StringIO(textwrap.dedent(text))


DEVICES = """\
apiVersion: r7s/v1
kind: Device
metadata: {name: scarlett}
spec: {usb: "1235:8210"}
---
apiVersion: r7s/v1
kind: Device
metadata: {name: midimix}
spec: {usb: "09E8:0031"}
"""


def rig(plugins: str = "", audio: str = "", midi: str = "", name: str = "r") -> str:
    def block(s: str, indent: int) -> str:
        return textwrap.indent(textwrap.dedent(s).strip("\n"), " " * indent) if s.strip() else ""

    out = f"""\
apiVersion: r7s/v1
kind: Rig
metadata: {{name: {name}}}
spec:
  plugins:
{block(plugins, 4) or "    []"}
  connections:
    audio:
{block(audio, 6) or "      []"}
    midi:
{block(midi, 6) or "      []"}
"""
    return out
