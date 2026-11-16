"""Generate ready-to-paste Rig plugin stanzas from plugin metadata."""

from __future__ import annotations

import json
import re

import yaml

from .lv2.model import Plugin, Port


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:40].rstrip("-")) or "plugin"


def default_value(port: Port) -> bool | int | float | str | None:
    d = port.default
    if d is None:
        d = port.minimum
    if d is None:
        return None
    if port.toggled:
        return d >= 0.5
    if port.enumeration and port.scale_points:
        for s in port.scale_points:
            if abs(s.value - d) < 1e-6:
                return s.label
    if port.integer:
        return int(round(d))
    return float(d)


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, float):
        return f"{v:g}" if "e" not in f"{v:g}" else repr(v)
    return str(v)


def _plain_key_ok(key: str) -> bool:
    """True if `key` reads back as the same string in any YAML 1.1/1.2 parser."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return False
    return yaml.safe_load(f"{key}: 0") == {key: 0}


def _comment(port: Port) -> str:
    parts = [port.name] if port.name and port.name != port.symbol else []
    if port.toggled:
        parts.append("toggle")
    elif port.enumeration and port.scale_points:
        parts.append("one of: " + " | ".join(s.label for s in port.scale_points))
    else:
        if port.minimum is not None or port.maximum is not None:
            lo = "" if port.minimum is None else f"{port.minimum:g}"
            hi = "" if port.maximum is None else f"{port.maximum:g}"
            unit = f" {port.unit}" if port.unit else ""
            parts.append(f"[{lo} .. {hi}{unit}]")
        elif port.unit:
            parts.append(port.unit)
        if port.integer:
            parts.append("integer")
        if port.scale_points:
            parts.append("named: " + ", ".join(f"{s.label}={s.value:g}" for s in port.scale_points))
    return ", ".join(parts)


def _connectable(plugin: Plugin) -> dict[str, list[str]]:
    groups = {
        "audio in": plugin.ports_of("audio", "input") + plugin.ports_of("cv", "input"),
        "audio out": plugin.ports_of("audio", "output") + plugin.ports_of("cv", "output"),
        "midi in": plugin.ports_of("midi", "input"),
        "midi out": plugin.ports_of("midi", "output"),
    }
    return {k: [p.symbol for p in v] for k, v in groups.items() if v}


def scaffold_dict(plugin: Plugin, plugin_id: str | None = None) -> dict:
    return {
        "id": plugin_id or slug(plugin.name),
        "lv2": plugin.uri,
        "parameters": {
            p.symbol: default_value(p)
            for p in plugin.ports
            if p.is_control_input and default_value(p) is not None
        },
        "ports": _connectable(plugin),
    }


def scaffold_yaml(plugin: Plugin, plugin_id: str | None = None, indent: int = 0, presets=()) -> str:
    """A `- id: ...` list item for spec.plugins, with ranges as comments."""
    pad = " " * indent
    pid = plugin_id or slug(plugin.name)
    lines = [
        f"{pad}# {plugin.name}" + (f" ({plugin.category})" if plugin.category else ""),
        f"{pad}- id: {pid}",
        f"{pad}  lv2: {json.dumps(plugin.uri)}",
    ]
    usable = [p for p in presets if not p.error]
    if usable:
        names = ", ".join(repr(p.label or p.uri) for p in usable[:6]) + (" …" if len(usable) > 6 else "")
        lines.append(f"{pad}  # preset: {usable[0].label or usable[0].uri!r}   # available: {names}")
    if plugin.has_ui:
        lines.append(f"{pad}  # ui: false              # has a native UI ({', '.join(plugin.ui_types)})")
    controls = [p for p in plugin.ports if p.is_control_input]
    rows = []
    for p in controls:
        v = default_value(p)
        val = "null" if v is None else _yaml_scalar(v)
        key = p.symbol if _plain_key_ok(p.symbol) else json.dumps(p.symbol)
        rows.append((f"{key}: {val}", _comment(p), v is None))
    if rows:
        lines.append(f"{pad}  parameters:")
        width = min(max(len(r[0]) for r in rows), 40)
        for text, comment, no_default in rows:
            prefix = "# " if no_default else ""
            line = f"{pad}    {prefix}{text.ljust(width)}"
            lines.append(f"{line}  # {comment}".rstrip() if comment else line.rstrip())
    conn = _connectable(plugin)
    if conn:
        lines.append(f"{pad}  # ports for connections ({pid}:<port>):")
        w = max(len(k) for k in conn) + 1
        for k, syms in conn.items():
            lines.append(f"{pad}  #   {(k + ':').ljust(w)} {', '.join(syms)}")
    return "\n".join(lines) + "\n"
