"""r7sd API logic, independent of the HTTP transport.

Every method returns (http_status, json_payload).
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from . import __version__
from .diagnostics import Diagnostic
from .endpoints import EndpointError, parse_device_port, parse_endpoint
from .hw.pipewire import HwGraph, PipeWireError
from .hw.resolve import resolve_device
from .loader import LoadResult, load_document
from .lv2.world import PluginWorld
from .reconciler import Reconciler
from .schema import KINDS
from .store import Store, _spec_key, merged
from .validate import validate_environment, validate_structure

KIND_ALIASES = {
    "rig": "Rig", "rigs": "Rig",
    "device": "Device", "devices": "Device", "dev": "Device",
    "activerig": "ActiveRig", "activerigs": "ActiveRig",
    "config": "Config", "configs": "Config",
}


def canonical_kind(k: str) -> str | None:
    if k in KINDS:
        return k
    return KIND_ALIASES.get(k.lower())


def _used_directions(resources: LoadResult) -> dict[str, set[str]]:
    """Device alias -> {"capture", "playback"} sides referenced by any rig."""
    used: dict[str, set[str]] = {}
    for r in resources.of_kind("Rig"):
        conns = r.resource.spec.connections
        for c in [*conns.audio, *conns.midi]:
            for raw in (c.from_, c.to):
                try:
                    ep = parse_endpoint(raw)
                except EndpointError:
                    continue
                dp = parse_device_port(ep.port)
                if dp is not None and not ep.is_inline:
                    used.setdefault(ep.node, set()).add("capture" if dp.role == "source" else "playback")
    return used


def _diag_payload(ds: list[Diagnostic]) -> list[dict]:
    return [d.to_dict() for d in ds]


class Api:
    def __init__(
        self,
        store: Store,
        reconciler: Reconciler,
        make_world: Callable[[LoadResult], PluginWorld],
        hw_snapshot: Callable[[], HwGraph],
        socket_path: str | None = None,
    ):
        self.store = store
        self.reconciler = reconciler
        self.make_world = make_world
        self.hw_snapshot = hw_snapshot
        self.socket_path = socket_path
        self.started = time.time()

    # --- admission ---------------------------------------------------------

    def _parse(self, docs: list[dict]) -> LoadResult:
        incoming = LoadResult()
        for d in docs:
            lines = {tuple(p): int(line) for p, line in d.get("lines", [])}
            load_document(d.get("data"), d.get("source") or "<api>", Path("/"), lines, incoming)
        return incoming

    def _admit(self, incoming: LoadResult, deleting: set[tuple[str, str]] = frozenset()) -> list[Diagnostic]:
        base = self.store.as_load_result(exclude=set(deleting))
        combined = merged(base, incoming.resources)
        diags = validate_structure(combined, strict_refs=True)
        if incoming.resources:
            world = self.make_world(combined)
            diags += [d for d in validate_environment(incoming, world) if d.severity != "info"]
            diags += self._hw_warnings(incoming, combined)
        return diags

    def _hw_warnings(self, incoming: LoadResult, combined: LoadResult) -> list[Diagnostic]:
        """Warn about Devices this change touches: incoming ones, and stored
        ones that an incoming Rig uses. Only sides some rig uses count."""
        used = _used_directions(combined)
        touched = {r.name for r in incoming.of_kind("Device")}
        touched |= set(_used_directions(incoming))
        devices = [r for r in combined.of_kind("Device") if r.name in touched]
        if not devices:
            return []
        try:
            hw = self.hw_snapshot()
        except PipeWireError as e:
            return [Diagnostic("warning", f"can't check hardware: {e}", tier=2)]
        out = []
        for r in devices:
            s = r.resource.spec
            res = resolve_device(hw, s.bus, s.hwid, s.serial, s.capture, s.playback)
            for key, msg in res.errors.items():
                if key != "device" and key not in used.get(r.name, set()):
                    continue  # no rig uses this side of the device
                field = ("spec", key) if key in ("capture", "playback") else ("spec", s.bus)
                out.append(r.diag("warning", msg + ("; connections will wait for it" if key == "device" else ""),
                                  field, tier=2))
        return out

    # --- endpoints -----------------------------------------------------------

    def apply(self, body: dict) -> tuple[int, dict]:
        incoming = self._parse(body.get("documents") or [])
        if not incoming.report.ok:
            return 422, {"diagnostics": _diag_payload(incoming.report.diagnostics)}
        if not incoming.resources:
            return 400, {"error": "no resources in request"}
        diags = self._admit(incoming)
        if any(d.severity == "error" for d in diags):
            return 422, {"diagnostics": _diag_payload(diags)}
        results = []
        dry = bool(body.get("dryRun"))
        for r in incoming.resources:
            if dry:
                old = self.store.get(r.kind, r.name)
                action = "created" if old is None else (
                    "unchanged" if _spec_key(old.manifest) == _spec_key(r.raw) else "configured")
            else:
                _, action = self.store.put(r.kind, r.name, r.raw, r.source)
            results.append({"ref": r.ref, "action": action})
        if not dry:
            self.reconciler.poke()
        return 200, {"results": results, "diagnostics": _diag_payload(diags), "dryRun": dry,
                     "revision": self.store.revision}

    def delete(self, refs: list[dict], dry: bool = False) -> tuple[int, dict]:
        keys = set()
        missing = []
        for ref in refs:
            kind = canonical_kind(ref.get("kind", ""))
            name = ref.get("name", "")
            if kind is None or self.store.get(kind, name) is None:
                missing.append(f"{ref.get('kind')}/{name}")
            else:
                keys.add((kind, name))
        if missing:
            return 404, {"error": "not found: " + ", ".join(missing)}
        diags = self._admit(LoadResult(), deleting=keys)
        errors = [d for d in diags if d.severity == "error"]
        if errors:
            return 422, {"diagnostics": _diag_payload(errors),
                         "error": "deleting would leave dangling references"}
        if not dry:
            for k in sorted(keys):
                self.store.delete(*k)
            self.reconciler.poke()
        return 200, {"results": [{"ref": f"{k}/{n}", "action": "deleted"} for k, n in sorted(keys)],
                     "dryRun": dry, "revision": self.store.revision}

    def _with_status(self, s) -> dict:
        d = s.to_dict()
        st = self.reconciler.status
        if s.kind == "Rig":
            d["status"] = st.get("rigs", {}).get(s.name)
        elif s.kind == "Device":
            d["status"] = st.get("devices", {}).get(s.name)
        elif s.kind == "ActiveRig":
            d["status"] = {"activeRig": st.get("activeRig")}
        return d

    def list(self, kind: str | None) -> tuple[int, dict]:
        k = None
        if kind and kind.lower() != "all":
            k = canonical_kind(kind)
            if k is None:
                return 400, {"error": f"unknown kind {kind!r}"}
        return 200, {"items": [self._with_status(s) for s in self.store.list(k)]}

    def get(self, kind: str, name: str) -> tuple[int, dict]:
        k = canonical_kind(kind)
        if k is None:
            return 400, {"error": f"unknown kind {kind!r}"}
        s = self.store.get(k, name)
        if s is None:
            return 404, {"error": f"{k}/{name} not found"}
        d = self._with_status(s)
        if k == "Rig":
            # Runtime events all concern the active rig's plugins and edges.
            d["events"] = [e.to_dict() for e in self.reconciler.events
                           if e.type in ("plugin", "connection", "reconciler")][-50:]
        return 200, d

    def status(self) -> tuple[int, dict]:
        st = dict(self.reconciler.status)
        st["starting"] = self.reconciler.reconciled_revision < 0  # no pass finished yet
        st["revision"] = self.store.revision
        st["reconciledRevision"] = self.reconciler.reconciled_revision
        st["daemon"] = {
            "version": __version__,
            "pid": os.getpid(),
            "uptime": round(time.time() - self.started, 1),
            "socket": self.socket_path,
            "stateDir": str(self.store.dir.parent),
            "plugins": len(self.reconciler.world.plugins) if self.reconciler.world else None,
        }
        return 200, st

    def events(self, since: float = 0.0) -> tuple[int, dict]:
        return 200, {"events": [e.to_dict() for e in self.reconciler.events if e.time > since]}

    def diff(self, body: dict) -> tuple[int, dict]:
        incoming = self._parse(body.get("documents") or [])
        if not incoming.report.ok:
            return 422, {"diagnostics": _diag_payload(incoming.report.diagnostics)}
        out = []
        for r in incoming.resources:
            old = self.store.get(r.kind, r.name)
            new_text = yaml.safe_dump(r.raw, sort_keys=False)
            if old is None:
                state, old_text = "new", ""
            else:
                old_text = yaml.safe_dump(old.manifest, sort_keys=False)
                state = "unchanged" if _spec_key(old.manifest) == _spec_key(r.raw) else "changed"
            text = "".join(difflib.unified_diff(
                old_text.splitlines(True), new_text.splitlines(True),
                fromfile=f"live/{r.ref}", tofile=f"{r.source}/{r.ref}"))
            out.append({"ref": r.ref, "state": state, "diff": text if state != "unchanged" else ""})
        return 200, {"items": out}

    def snapshot(self, rig: str) -> tuple[int, dict]:
        """The Rig with parameter values as they are now on the running plugins."""
        s = self.store.get("Rig", rig)
        if s is None:
            return 404, {"error": f"Rig/{rig} not found"}
        if self.reconciler.status.get("activeRig") != rig:
            return 409, {"error": f"Rig/{rig} is not active; only the active rig has live values"}
        manifest = copy.deepcopy(s.manifest)
        changed = []
        desired = self.reconciler.desired
        for p in (manifest.get("spec") or {}).get("plugins") or []:
            live = self.reconciler.live_params(p.get("id"))
            want = desired.plugins.get(p.get("id")) if desired else None
            if live is None or want is None or want.plugin is None:
                continue
            params = p.setdefault("parameters", {}) or {}
            p["parameters"] = params
            defaults = {q.symbol: q.default for q in want.plugin.ports}
            for sym, val in sorted(live.items()):
                port = want.plugin.port(sym)
                in_spec = sym in params
                if not in_spec and defaults.get(sym) is not None and abs(defaults[sym] - val) < 1e-9:
                    continue
                if abs(want.params.get(sym, float("nan")) - val) < 1e-9 and in_spec:
                    continue
                shown = _shown_value(port, val)
                if isinstance(params.get(sym), dict):
                    params[sym]["value"] = shown
                else:
                    params[sym] = shown
                changed.append(f"{p['id']}.{sym}")
        return 200, {"manifest": manifest, "changed": changed,
                     "yaml": yaml.safe_dump(manifest, sort_keys=False)}


def _shown_value(port, val: float):
    if port is not None:
        if port.toggled:
            return val >= 0.5
        if port.scale_points:
            for sp in port.scale_points:
                if abs(sp.value - val) < 1e-6:
                    return sp.label
        if port.integer:
            return int(round(val))
    return float(f"{val:.6g}")


def dumps(payload) -> bytes:
    return json.dumps(payload, default=str).encode()

