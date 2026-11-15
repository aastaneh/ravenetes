"""The graph the web UI renders, built from the store and reconciler state.

Nodes are plugins and device *sides*: a device used for both input and
output becomes "scarlett (in)" on the left and "scarlett (out)" on the right,
so the graph always reads left to right. Edges are port-level; the UI groups
them per node pair for its plugin view.
"""

from __future__ import annotations

import time

from ..endpoints import EndpointError, parse_device_port, parse_endpoint
from ..schema import ParamMapping, param_initial

EPS = 1e-6


def _num(v):
    return None if v is None else round(float(v), 6)


def _shown(port, value):
    """Display form of a float value: scale-point label, bool, int, or number."""
    if value is None or port is None:
        return value
    if port.toggled:
        return value >= 0.5
    for sp in port.scale_points:
        if abs(sp.value - value) < 1e-6:
            return sp.label
    if port.integer:
        return int(round(value))
    return round(value, 4)


def _params(pspec, plugin, managed, want) -> list[dict]:
    out = []
    applied = managed.applied if managed else {}
    live = managed.live if managed else {}
    for port in plugin.ports:
        if not port.is_control_input:
            continue
        v = pspec.parameters.get(port.symbol)
        mapping = v if isinstance(v, ParamMapping) and v.cc is not None else None
        spec_val = param_initial(v) if v is not None else None
        lv, ap = live.get(port.symbol), applied.get(port.symbol)
        if want is not None and lv is None:
            lv = want.params.get(port.symbol)
        drift = lv is not None and ap is not None and abs(lv - ap) > EPS
        out.append({
            "symbol": port.symbol, "name": port.name, "unit": port.unit,
            "min": _num(port.minimum), "max": _num(port.maximum), "default": _num(port.default),
            "spec": spec_val, "inSpec": v is not None,
            "live": _shown(port, lv), "liveRaw": _num(lv), "applied": _num(ap),
            "drift": drift,
            "cc": None if mapping is None else {
                "cc": mapping.cc, "channel": mapping.channel, "device": mapping.device,
                "min": mapping.min, "max": mapping.max},
            "labels": [sp.label for sp in port.scale_points] or None,
            "toggle": port.toggled,
        })
    return out


def build_graph(reconciler, store, rig_name: str | None = None) -> dict:
    status = reconciler.status or {}
    active = status.get("activeRig")
    rigs = [s.name for s in store.list("Rig")]
    name = rig_name or active
    base = {
        "time": time.time(),
        "activeRig": active,
        "rigs": rigs,
        "jack": status.get("jack"),
        "xruns": status.get("xruns", 0),
        "cpuLoad": status.get("cpuLoad"),
        "backend": status.get("backend"),
        "problems": status.get("problems") or [],
        "midi": status.get("midi"),
        "revision": status.get("revision"),
    }
    if name is None or name not in rigs:
        return {**base, "rig": None, "nodes": [], "edges": []}

    resources = store.as_load_result()
    rig_res = next(r for r in resources.of_kind("Rig") if r.name == name)
    rig = rig_res.resource
    is_active = name == active
    rst = (status.get("rigs") or {}).get(name) or {}
    pstat = rst.get("plugins") or {}
    dstat = status.get("devices") or {}
    world = reconciler.world
    desired = reconciler.desired if is_active else None

    nodes: dict[str, dict] = {}
    for p in rig.spec.plugins:
        plugin = world.get(p.lv2) if (world and p.lv2) else None
        st = pstat.get(p.id) or {}
        managed = reconciler.managed.get(p.id) if is_active else None
        want = desired.plugins.get(p.id) if desired else None
        ports_in, ports_out = [], []
        if plugin is not None:
            for q in plugin.ports:
                if q.type in ("audio", "cv", "midi"):
                    entry = {"symbol": q.symbol, "name": q.name, "type": "midi" if q.type == "midi" else "audio"}
                    (ports_in if q.direction == "input" else ports_out).append(entry)
        nodes[f"p:{p.id}"] = {
            "id": f"p:{p.id}", "kind": "plugin", "label": p.id,
            "title": plugin.name if plugin else (p.lv2 or p.vst3 or p.clap),
            "uri": p.lv2 or p.vst3 or p.clap, "category": plugin.category if plugin else None,
            "installed": plugin is not None,
            "state": st.get("state") or ("Inactive" if not is_active else "Waiting"),
            "pid": st.get("pid"), "restarts": st.get("restarts", 0),
            "uptime": st.get("uptime"), "lastError": st.get("lastError"), "lastExit": st.get("lastExit"),
            "log": st.get("log"),
            "inputs": ports_in, "outputs": ports_out,
            "params": _params(p, plugin, managed, want) if plugin else [],
        }
        nodes[f"p:{p.id}"]["drift"] = sum(1 for x in nodes[f"p:{p.id}"]["params"] if x["drift"])

    def device_node(alias: str, side: str, port: str, kind: str) -> str:
        nid = f"d:{alias}:{side}"
        n = nodes.get(nid)
        if n is None:
            ds = dstat.get(alias) or {}
            n = nodes[nid] = {
                "id": nid, "kind": "device", "side": side, "label": alias,
                "title": ds.get("device") or alias,
                "present": ds.get("present") if ds else None,
                "endpoint": ds.get("capture" if side == "in" else "playback") if ds else None,
                "errors": ds.get("errors") or {},
                "inputs": [], "outputs": [],
            }
        lst = n["outputs"] if side == "in" else n["inputs"]
        if not any(x["symbol"] == port for x in lst):
            lst.append({"symbol": port, "name": port, "type": kind})
        return nid

    status_conns = {(c["section"], c["from"], c["to"]): c for c in rst.get("connections") or []}
    edges = []
    for section in ("audio", "midi"):
        for i, c in enumerate(getattr(rig.spec.connections, section)):
            ends = []
            for raw, role in ((c.from_, "from"), (c.to, "to")):
                try:
                    ep = parse_endpoint(raw)
                except EndpointError:
                    ends = None
                    break
                if f"p:{ep.node}" in nodes and not ep.is_inline:
                    ends.append((f"p:{ep.node}", ep.port))
                else:
                    dp = parse_device_port(ep.port)
                    kind = dp.kind if dp else section
                    ends.append((device_node(ep.node, "in" if role == "from" else "out", ep.port, kind), ep.port))
            if not ends:
                continue
            cs = status_conns.get((section, c.from_, c.to)) or {}
            edges.append({
                "id": f"{section}{i}", "section": section,
                "from": ends[0][0], "fromPort": ends[0][1], "to": ends[1][0], "toPort": ends[1][1],
                "raw": [c.from_, c.to],
                "state": cs.get("state") or ("Inactive" if not is_active else "Pending"),
                "reason": cs.get("reason"), "jack": [cs.get("jackFrom"), cs.get("jackTo")],
            })

    return {
        **base,
        "rig": name, "active": is_active,
        "phase": rst.get("phase", "Inactive"),
        "generation": rst.get("generation"), "observedGeneration": rst.get("observedGeneration"),
        "rigXruns": rst.get("xruns", 0),
        "errors": rst.get("errors") or [],
        "drift": sum(n.get("drift", 0) for n in nodes.values()),
        "nodes": list(nodes.values()),
        "edges": edges,
    }
