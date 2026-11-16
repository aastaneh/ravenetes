"""Desired runtime state: what should be running and wired, given the store.

Pure: inputs are validated resources, plugin metadata, and a hardware
snapshot; the output says which plugin instances should exist with which
parameter values, and which JACK edges should exist (or why they can't yet).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from .backend.base import InstanceSpec, client_name
from .endpoints import EndpointError, parse_device_port, parse_endpoint
from .hw.inventory import Hardware
from .hw.resolve import DeviceResolution
from .loader import LoadResult
from .lv2.model import Plugin
from .lv2.world import PluginWorld
from .schema import Device, ParamMapping, Rig, param_initial
from .validate import SECTIONS, scale_point_value


@dataclass
class DesiredPlugin:
    spec: InstanceSpec
    params: dict[str, float]  # every control input: spec value or plugin default
    explicit: set[str] = field(default_factory=set)  # symbols set in the Rig
    plugin: Plugin | None = None
    # What the plugin holds right after launch: preset values over defaults.
    baseline: dict[str, float] = field(default_factory=dict)
    preset_label: str | None = None
    cc: dict[str, ParamMapping] = field(default_factory=dict)  # symbol -> CC mapping

    def launch_params(self) -> dict[str, float]:
        """Values to pass with -c: explicit ones and anything that differs from
        what the plugin will hold anyway (its defaults, or its preset)."""
        out = {}
        for sym, v in self.params.items():
            base = self.baseline.get(sym)
            if sym in self.explicit or base is None or abs(base - v) > 1e-9:
                out[sym] = v
        return out


@dataclass
class DesiredEdge:
    section: str
    index: int
    raw_from: str
    raw_to: str
    src: str | None = None  # JACK port names, once resolvable
    dst: str | None = None
    reason: str | None = None  # why it can't be resolved

    @property
    def resolved(self) -> bool:
        return self.src is not None and self.dst is not None


@dataclass
class Desired:
    rig: str | None = None
    generation: int | None = None
    plugins: dict[str, DesiredPlugin] = field(default_factory=dict)
    edges: list[DesiredEdge] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    devices: dict[str, DeviceResolution] = field(default_factory=dict)


def to_float(plugin: Plugin, symbol: str, value) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        port = plugin.port(symbol)
        return scale_point_value(port, value) if port else None
    return float(value)


def resolve_devices(
    resources: LoadResult, hw: Hardware | None, hw_error: str | None
) -> dict[str, DeviceResolution]:
    out = {}
    for r in resources.of_kind("Device"):
        d: Device = r.resource
        if hw is None:
            out[r.name] = DeviceResolution(errors={"device": hw_error or "hardware state unknown"})
        else:
            s = d.spec
            out[r.name] = hw.resolve_spec(s)
    return out


def compute_desired(
    resources: LoadResult,
    world: PluginWorld,
    hw: Hardware | None,
    hw_error: str | None = None,
    jack_ports: Collection[str] | None = None,
    generations: dict[str, int] | None = None,
) -> Desired:
    desired = Desired(devices=resolve_devices(resources, hw, hw_error))
    actives = resources.of_kind("ActiveRig")
    if not actives:
        return desired
    rig_name = actives[0].resource.spec.rig
    rigs = {r.name: r for r in resources.of_kind("Rig")}
    if rig_name not in rigs:
        desired.errors.append(f"ActiveRig points at Rig {rig_name!r}, which doesn't exist")
        return desired
    desired.rig = rig_name
    desired.generation = (generations or {}).get(rig_name)
    rig: Rig = rigs[rig_name].resource

    for p in rig.spec.plugins:
        if p.format != "lv2":
            desired.errors.append(f"{p.id}: {p.format} plugins aren't supported")
            continue
        plugin = world.get(p.lv2)
        if plugin is None:
            desired.errors.append(f"{p.id}: plugin {p.lv2!r} is no longer installed")
            continue
        params: dict[str, float] = {
            q.symbol: q.default for q in plugin.ports if q.is_control_input and q.default is not None
        }
        preset = None
        if p.preset:
            preset, why = world.find_preset(p.lv2, p.preset)
            if preset is None or preset.error:
                desired.errors.append(f"{p.id}: {why or preset.error}")
                continue
            controls = {q.symbol for q in plugin.ports if q.is_control_input}
            params.update({s: v for s, v in preset.port_values.items() if s in controls})
        baseline = dict(params)
        explicit = set()
        cc = {}
        for sym, v in p.parameters.items():
            if isinstance(v, ParamMapping) and v.cc is not None:
                cc[sym] = v
            initial = param_initial(v)
            if initial is None:
                continue
            f = to_float(plugin, sym, initial)
            if f is not None:
                params[sym] = f
                explicit.add(sym)
        # `ui` is accepted but not acted on in v1, so toggling it never restarts a plugin.
        spec = InstanceSpec(
            id=p.id, uri=p.lv2, bundle=p.bundle,
            preset_path=preset.path if preset else None,
            preset_uri=preset.uri if preset and not preset.path else None,
        )
        desired.plugins[p.id] = DesiredPlugin(
            spec=spec, params=params, explicit=explicit, plugin=plugin, cc=cc, baseline=baseline,
            preset_label=(preset.label or preset.uri) if preset else None,
        )

    inline_cache: dict[str, DeviceResolution] = {}

    def end(raw: str) -> tuple[str | None, str | None]:
        try:
            ep = parse_endpoint(raw)
        except EndpointError as e:
            return None, str(e)
        if ep.node in desired.plugins:
            return f"{client_name(ep.node)}:{ep.port}", None
        if any(pp.id == ep.node for pp in rig.spec.plugins):
            return None, f"plugin {ep.node!r} isn't running"
        dp = parse_device_port(ep.port)
        if dp is None:
            return None, f"{ep.port!r} is not a device port"
        if ep.is_inline:
            res = inline_cache.get(ep.node)
            if res is None:
                res = (
                    hw.resolve(ep.inline_bus, ep.inline_id)
                    if hw is not None
                    else DeviceResolution(errors={"device": hw_error or "hardware state unknown"})
                )
                inline_cache[ep.node] = res
        else:
            res = desired.devices.get(ep.node)
            if res is None:
                return None, f"no Device named {ep.node!r}"
        name, err = res.jack_port(dp, ep.port, jack_ports)
        return name, (f"{ep.node}: {err}" if err else None)

    for section in SECTIONS:
        for i, c in enumerate(getattr(rig.spec.connections, section)):
            edge = DesiredEdge(section, i, c.from_, c.to)
            edge.src, e1 = end(c.from_)
            edge.dst, e2 = end(c.to)
            edge.reason = e1 or e2
            desired.edges.append(edge)
    return desired


def cc_routes(desired: Desired) -> tuple[list, list[str]]:
    """(routes, problems) for every CC mapping in the active rig."""
    from .midi_cc import route_for  # noqa: PLC0415

    routes, problems = [], []
    for pid, want in desired.plugins.items():
        for sym, m in want.cc.items():
            port = want.plugin.port(sym) if want.plugin else None
            res = desired.devices.get(m.device)
            if port is None:
                continue  # admission already rejected unknown parameters
            if res is None:
                problems.append(f"{pid}.{sym}: no Device named {m.device!r}")
                continue
            sources = res.seq_sources()
            if not sources:
                why = res.errors.get("device") or res.midi_error or f"{res.label or m.device} has no MIDI inputs"
                problems.append(f"{pid}.{sym}: CC {m.cc} from {m.device}: {why}")
                continue
            for src in sources:
                routes.append(route_for(port, src, m.cc, m.channel, pid, m.min, m.max))
    return routes, problems
