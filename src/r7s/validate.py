"""Admission checks.

Tier 1 (structural) needs nothing but the resources themselves.
Tier 2 (environmental) checks rigs against installed plugin metadata; device
presence checks will join it once r7s can see the live JACK graph.
"""

from __future__ import annotations

import difflib
import math
import re
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import Diagnostic
from .endpoints import EndpointError, parse_device_port, parse_endpoint
from .loader import Loaded, LoadResult
from .lv2.model import Plugin, Port
from .lv2.world import PluginWorld
from .schema import SUPPORTED_FORMATS, Backend, Display, ParamMapping, Rig, param_initial

SECTIONS = ("audio", "midi")
EPS = 1e-6
# jalv's console parses `preset URI` with sscanf("%1023[a-zA-Z0-9_:/-.#]").
PRESET_URI_RE = re.compile(r"^[A-Za-z0-9_:/\-.#]{1,1023}$")


@dataclass
class Known:
    """Names that exist outside the input set (e.g. in the daemon's store)."""

    devices: frozenset[str] = frozenset()
    rigs: frozenset[str] = frozenset()


def _a(word: str) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


def _close(word: str, options: Iterable[str]) -> str:
    match = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f"; did you mean {match[0]!r}?" if match else ""


# --- tier 1 ----------------------------------------------------------------


def validate_structure(
    result: LoadResult, known: Known = Known(), strict_refs: bool = False
) -> list[Diagnostic]:
    """Structural checks. With strict_refs, unresolved references are errors
    (apply time); otherwise warnings, because the daemon may define them."""
    ref_sev = "error" if strict_refs else "warning"
    devices = {r.name for r in result.of_kind("Device")} | known.devices
    rigs = {r.name for r in result.of_kind("Rig")} | known.rigs
    out: list[Diagnostic] = []

    for r in result.of_kind("Rig"):
        out.extend(_check_rig(r, devices, ref_sev))

    actives = result.of_kind("ActiveRig")
    for extra in actives[1:]:
        out.append(extra.diag("error", f"only one ActiveRig allowed; also defined as {actives[0].ref}"))
    for a in actives:
        if a.resource.spec.rig not in rigs:
            out.append(
                a.diag(ref_sev, f"no Rig named {a.resource.spec.rig!r}" + _close(a.resource.spec.rig, rigs), ("spec", "rig"))
            )

    configs = result.of_kind("Config")
    for extra in configs[1:]:
        out.append(extra.diag("error", f"only one Config allowed; also defined as {configs[0].ref}"))
    for c in configs:
        if c.resource.spec.backend == Backend.mod_host:
            out.append(
                c.diag("warning", "the mod-host backend isn't implemented yet; r7s will use jalv", ("spec", "backend"))
            )
        if c.resource.spec.display == Display.always:
            out.append(c.diag("warning", NO_NATIVE_UI, ("spec", "display")))
    return out


NO_NATIVE_UI = ("native plugin windows aren't supported yet: jalv can't show one and stay "
                "controllable, so plugins run headless (planned with the Carla backend)")


def _check_rig(r: Loaded, devices: set[str], ref_sev: str) -> list[Diagnostic]:
    rig: Rig = r.resource
    out: list[Diagnostic] = []
    ids: dict[str, int] = {}

    for i, p in enumerate(rig.spec.plugins):
        path = ("spec", "plugins", i)
        if p.id in ids:
            out.append(r.diag("error", f"duplicate plugin id {p.id!r} (also plugins[{ids[p.id]}])", (*path, "id")))
            continue
        ids[p.id] = i
        if p.id in devices:
            out.append(r.diag("error", f"plugin id {p.id!r} collides with a Device of the same name", (*path, "id")))
        if p.ui:
            out.append(r.diag("warning", NO_NATIVE_UI, (*path, "ui")))
        if p.format not in SUPPORTED_FORMATS:
            out.append(
                r.diag("error", f"{p.format} plugins aren't supported yet; this version hosts LV2 only", (*path, p.format))
            )
        for sym, v in p.parameters.items():
            if isinstance(v, ParamMapping) and v.device and v.device not in devices:
                out.append(
                    r.diag(ref_sev, f"no Device named {v.device!r}" + _close(v.device, devices),
                           (*path, "parameters", sym, "device"))
                )

    edges: dict[str, set[str]] = {pid: set() for pid in ids}
    seen_edges: dict[tuple[str, str, str], int] = {}

    for section in SECTIONS:
        for j, conn in enumerate(getattr(rig.spec.connections, section)):
            cpath = ("spec", "connections", section, j)
            ends = {}
            for field, role in (("from", "source"), ("to", "sink")):
                fpath = (*cpath, field)
                raw = conn.from_ if field == "from" else conn.to
                try:
                    ep = parse_endpoint(raw)
                except EndpointError as e:
                    out.append(r.diag("error", str(e), fpath))
                    continue
                ends[field] = ep
                if ep.is_inline or ep.node in devices:
                    dp = parse_device_port(ep.port)
                    if dp is None:
                        out.append(
                            r.diag("error",
                                   f"{ep.port!r} is not a device port; use capture_<n>/playback_<n> "
                                   "(or _l/_r) for audio, midi_capture/midi_playback for MIDI", fpath)
                        )
                    elif dp.kind != section:
                        out.append(r.diag("error", f"{ep.port!r} is a {dp.kind} port but is listed under {section}", fpath))
                    elif dp.role != role:
                        want = "a capture port" if role == "source" else "a playback port"
                        out.append(r.diag("error", f"'{field}' needs {want}; {ep.port!r} goes the other way", fpath))
                elif ep.node not in ids:
                    out.append(
                        r.diag(ref_sev,
                               f"{ep.node!r} is not a plugin in this rig or a known Device"
                               + _close(ep.node, [*ids, *devices]), fpath)
                    )
            if len(ends) < 2:
                continue
            src, dst = ends["from"], ends["to"]
            key = (section, f"{src.node}:{src.port}", f"{dst.node}:{dst.port}")
            if key[1] == key[2]:
                out.append(r.diag("error", "a port can't connect to itself", cpath))
                continue
            if key in seen_edges:
                out.append(r.diag("warning", f"duplicate connection (same as {section}[{seen_edges[key]}])", cpath))
                continue
            seen_edges[key] = j
            if src.node in ids and dst.node in ids:
                edges[src.node].add(dst.node)

    for cycle in _find_cycles(edges, order=list(ids)):
        out.append(r.diag("error", "feedback loop: " + " → ".join(cycle), ("spec", "connections")))
    return out


def _find_cycles(edges: dict[str, set[str]], order: list[str]) -> list[list[str]]:
    """One representative cycle per strongly-connected loop, via DFS."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)
    stack: list[str] = []
    cycles: list[list[str]] = []
    reported: set[frozenset[str]] = set()

    def visit(n: str) -> None:
        color[n] = GREY
        stack.append(n)
        for m in sorted(edges[n], key=order.index):
            if color[m] == GREY:
                cyc = stack[stack.index(m):] + [m]
                key = frozenset(cyc)
                if key not in reported:
                    reported.add(key)
                    cycles.append(cyc)
            elif color[m] == WHITE:
                visit(m)
        stack.pop()
        color[n] = BLACK

    for n in order:
        if color[n] == WHITE:
            visit(n)
    return cycles


# --- tier 2 ----------------------------------------------------------------


def rig_bundles(result: LoadResult) -> list[Path]:
    """Local `bundle:` paths from every rig, resolved against their files."""
    out = []
    for r in result.of_kind("Rig"):
        for p in r.resource.spec.plugins:
            if p.bundle:
                out.append(resolve_bundle(r, p.bundle))
    return out


def resolve_bundle(r: Loaded, bundle: str) -> Path:
    return (r.base_dir / os.path.expanduser(bundle)).resolve()


def validate_environment(result: LoadResult, world: PluginWorld) -> list[Diagnostic]:
    """Tier 2 plugin checks. Hardware is checked by validate_hardware."""
    out: list[Diagnostic] = []
    for r in result.of_kind("Rig"):
        out.extend(_check_rig_env(r, world))
    return out


def used_device_sides(resources: LoadResult) -> dict[str, set[str]]:
    """Device alias -> sides rigs use: capture, playback, midi_capture,
    midi_playback, and cc (a CC mapping reads the device's MIDI inputs)."""
    used: dict[str, set[str]] = {}
    for r in resources.of_kind("Rig"):
        spec = r.resource.spec
        for c in [*spec.connections.audio, *spec.connections.midi]:
            for raw in (c.from_, c.to):
                try:
                    ep = parse_endpoint(raw)
                except EndpointError:
                    continue
                dp = parse_device_port(ep.port)
                if dp is None or ep.is_inline:
                    continue
                side = "capture" if dp.role == "source" else "playback"
                used.setdefault(ep.node, set()).add(side if dp.kind == "audio" else f"midi_{side}")
        for p in spec.plugins:
            for v in p.parameters.values():
                if isinstance(v, ParamMapping) and v.cc is not None and v.device:
                    used.setdefault(v.device, set()).add("cc")
    return used


def validate_hardware(incoming: LoadResult, combined: LoadResult, hw, hw_error: str | None = None) -> list[Diagnostic]:
    """Warnings (never errors) about the hardware the change touches.

    Checks incoming Devices, plus stored Devices an incoming Rig uses, and
    only the sides some rig uses. Hardware can be plugged in later; the rig
    waits for it.
    """
    used = used_device_sides(combined)
    touched = {r.name for r in incoming.of_kind("Device")} | set(used_device_sides(incoming))
    devices = [r for r in combined.of_kind("Device") if r.name in touched]
    if not devices:
        return []
    if hw is None:
        return [Diagnostic("warning", f"can't check hardware: {hw_error or 'unavailable'}", tier=2)]
    out = []
    for r in devices:
        s = r.resource.spec
        res = hw.resolve_spec(s)
        sides = used.get(r.name, set())
        if "device" in res.errors:
            out.append(r.diag("warning", res.errors["device"] + "; connections will wait for it",
                              ("spec", s.bus), tier=2))
            continue
        for side in ("capture", "playback"):
            if side in sides and side in res.errors:
                out.append(r.diag("warning", res.errors[side], ("spec", side), tier=2))
        for side, word in (("midi_capture", "inputs"), ("midi_playback", "outputs")):
            if side not in sides and not (side == "midi_capture" and "cc" in sides):
                continue
            ports = (res.midi or {}).get(side.removeprefix("midi_"))
            if res.midi is None:
                out.append(r.diag("warning", res.midi_error or f"MIDI ports of {res.label} are unknown",
                                  ("spec", s.bus), tier=2))
                break
            if not ports:
                why = "CC mappings and MIDI connections" if side == "midi_capture" else "MIDI connections"
                out.append(r.diag("warning", f"{res.label} has no MIDI {word}; {why} from it won't work",
                                  ("spec", s.bus), tier=2))
    return out


def _d(r: Loaded, sev: str, msg: str, path) -> Diagnostic:
    return r.diag(sev, msg, path, tier=2)


def _check_rig_env(r: Loaded, world: PluginWorld) -> list[Diagnostic]:
    rig: Rig = r.resource
    out: list[Diagnostic] = []
    resolved: dict[str, Plugin] = {}

    for i, p in enumerate(rig.spec.plugins):
        path = ("spec", "plugins", i)
        if p.format != "lv2":
            continue
        if p.bundle:
            bpath = resolve_bundle(r, p.bundle)
            if not (bpath / "manifest.ttl").is_file():
                out.append(_d(r, "error", f"bundle {str(bpath)!r} not found (no manifest.ttl)", (*path, "bundle")))
                continue
        plugin = world.get(p.lv2)
        if plugin is None:
            hint = world.suggest(p.lv2)
            msg = f"plugin {p.lv2!r} is not installed in the plugin paths"
            if hint:
                msg += f"; did you mean {hint[0]!r}?"
            else:
                msg += " (" + ", ".join(world.paths) + ")"
            out.append(_d(r, "error", msg, (*path, "lv2")))
            continue
        if p.bundle and Path(plugin.bundle) != resolve_bundle(r, p.bundle):
            out.append(
                _d(r, "error", f"bundle {p.bundle!r} doesn't provide {p.lv2!r}", (*path, "bundle"))
            )
            continue
        resolved[p.id] = plugin
        if p.preset:
            preset, why = world.find_preset(p.lv2, p.preset)
            if preset is None:
                out.append(_d(r, "error", why, (*path, "preset")))
            elif preset.error:
                out.append(_d(r, "error", f"preset {p.preset!r} can't be read: {preset.error}", (*path, "preset")))
            elif preset.path is None and not PRESET_URI_RE.match(preset.uri):
                out.append(_d(r, "error", f"preset URI {preset.uri!r} has characters jalv's console can't take",
                              (*path, "preset")))
        for sym, v in p.parameters.items():
            out.extend(_check_param(r, (*path, "parameters", sym), plugin, sym, v))

    for section in SECTIONS:
        for j, conn in enumerate(getattr(rig.spec.connections, section)):
            for field, direction in (("from", "output"), ("to", "input")):
                raw = conn.from_ if field == "from" else conn.to
                try:
                    ep = parse_endpoint(raw)
                except EndpointError:
                    continue  # tier 1 reported it
                plugin = resolved.get(ep.node)
                if plugin is None:
                    continue
                fpath = ("spec", "connections", section, j, field)
                port = plugin.port(ep.port)
                ok_types = ("audio", "cv") if section == "audio" else ("midi",)
                if port is None:
                    candidates = [
                        q.symbol for q in plugin.ports if q.direction == direction and q.type in ok_types
                    ]
                    msg = f"{plugin.name} has no port {ep.port!r}"
                    if candidates:
                        msg += f"; {section} {direction}s: {', '.join(candidates)}"
                    else:
                        msg += f"; it has no {section} {direction}s"
                    out.append(_d(r, "error", msg, fpath))
                    continue
                if port.type not in ok_types:
                    out.append(
                        _d(r, "error",
                           f"{ep.node}:{port.symbol} is {_a(port.type)} {port.direction}; "
                           f"{section} connections need {_a(section)} port", fpath)
                    )
                elif port.direction != direction:
                    out.append(
                        _d(r, "error",
                           f"'{field}' needs an {direction}; {ep.node}:{port.symbol} is an {port.direction}", fpath)
                    )
    return out


def _fmt(x: float) -> str:
    return f"{x:g}"


def _range_str(port: Port) -> str:
    lo = "-inf" if port.minimum is None else _fmt(port.minimum)
    hi = "inf" if port.maximum is None else _fmt(port.maximum)
    unit = f" {port.unit}" if port.unit else ""
    return f"[{lo} .. {hi}{unit}]"


def _check_param(r: Loaded, path, plugin: Plugin, sym: str, v) -> list[Diagnostic]:
    port = plugin.port(sym)
    if port is None:
        controls = [p.symbol for p in plugin.ports if p.is_control_input]
        return [_d(r, "error", f"{plugin.name} has no parameter {sym!r}" + _close(sym, controls), path)]
    if not port.is_control_input:
        return [_d(r, "error", f"{sym!r} is {_a(port.type)} {port.direction}, not a control input", path)]

    out = []
    initial = param_initial(v)
    if initial is not None:
        vpath = (*path, "value") if isinstance(v, ParamMapping) else path
        if (msg := check_value(port, initial)) is not None:
            out.append(_d(r, "error", msg, vpath))
    if isinstance(v, ParamMapping) and v.cc is not None:
        for bound in ("min", "max"):
            b = getattr(v, bound)
            if b is not None and not _in_range(port, b):
                out.append(_d(r, "error", f"CC {bound} {_fmt(b)} is outside {sym} {_range_str(port)}", (*path, bound)))
    return out


def _in_range(port: Port, x: float) -> bool:
    if port.minimum is not None and x < port.minimum - EPS:
        return False
    if port.maximum is not None and x > port.maximum + EPS:
        return False
    return True


def check_value(port: Port, v) -> str | None:
    """Why `v` isn't a valid value for `port`, or None if it is."""
    if isinstance(v, bool):
        if port.toggled or (port.minimum == 0 and port.maximum == 1):
            return None
        return f"{port.symbol} is not a toggle; give a number in {_range_str(port)}"
    if isinstance(v, str):
        if not port.scale_points:
            return f"{port.symbol} takes a number in {_range_str(port)}; labels only work on ports with named values"
        if scale_point_value(port, v) is None:
            labels = ", ".join(s.label for s in port.scale_points)
            return f"{v!r} is not a value of {port.symbol}; choose one of: {labels}"
        return None
    x = float(v)
    if not math.isfinite(x):
        return f"{port.symbol} must be a finite number"
    if not _in_range(port, x):
        return f"{_fmt(x)} is outside {port.symbol} {_range_str(port)}"
    if port.integer and abs(x - round(x)) > EPS:
        return f"{port.symbol} takes whole numbers; got {_fmt(x)}"
    if port.enumeration and port.scale_points and not any(abs(s.value - x) <= EPS for s in port.scale_points):
        choices = ", ".join(f"{s.label} ({_fmt(s.value)})" for s in port.scale_points)
        return f"{_fmt(x)} is not a value of {port.symbol}; choose one of: {choices}"
    return None


def scale_point_value(port: Port, label: str) -> float | None:
    for s in port.scale_points:
        if s.label == label:
            return s.value
    folded = label.casefold()
    for s in port.scale_points:
        if s.label.casefold() == folded:
            return s.value
    return None
