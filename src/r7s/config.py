"""Plugin path resolution across CLI flags, Config resources, and defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .loader import LoadResult, load
from .lv2.world import default_plugin_paths, env_plugin_paths


def user_config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "r7s" / "config.yaml"


@dataclass
class PluginPaths:
    paths: list[str]
    origin: str  # human-readable: where the paths came from


def resolve_plugin_paths(
    cli_paths: tuple[str, ...] | list[str] = (),
    inputs: LoadResult | None = None,
) -> PluginPaths:
    """Precedence: --plugin-path, Config in the inputs, user config file,
    $LV2_PATH, built-in defaults."""
    if cli_paths:
        return PluginPaths(list(cli_paths), "--plugin-path")
    if inputs is not None:
        for c in inputs.of_kind("Config"):
            if c.resource.spec.plugin_paths is not None:
                return PluginPaths(_relative_to(c.resource.spec.plugin_paths, c.base_dir), f"Config in {c.source}")
    cfg = user_config_file()
    if cfg.is_file():
        res = load([str(cfg)])
        for c in res.of_kind("Config"):
            if c.resource.spec.plugin_paths is not None:
                return PluginPaths(_relative_to(c.resource.spec.plugin_paths, c.base_dir), str(cfg))
    if (env := env_plugin_paths()) is not None:
        return PluginPaths(env, "$LV2_PATH")
    return PluginPaths(default_plugin_paths(), "defaults")


def _relative_to(paths: list[str], base: Path) -> list[str]:
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        out.append(p if os.path.isabs(p) else str((base / p).resolve()))
    return out
