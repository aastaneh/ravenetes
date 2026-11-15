"""r7sctl commands that talk to r7sd, plus hardware discovery."""

from __future__ import annotations

import copy
import json
import sys
import time

import click
import yaml

from .api import canonical_kind
from .client import Client, DaemonUnavailable
from .hw.pipewire import HwGraph, PipeWireError
from .hw.pipewire import snapshot as hw_snapshot
from .hw.resolve import preferred_selector
from .loader import InputError, Loaded, load
from .scaffold import slug
from .validate import resolve_bundle


def _err(msg: str) -> None:
    click.echo(f"r7sctl: {msg}", err=True)


def _socket_option(f):
    return click.option("--socket", envvar="R7S_SOCKET", metavar="PATH",
                        help="r7sd socket (default $XDG_RUNTIME_DIR/r7s.sock).")(f)


def _call(client: Client, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    try:
        return client.request(method, path, body)
    except DaemonUnavailable as e:
        _err(str(e))
        sys.exit(3)


def _print_diags(diags: list[dict]) -> None:
    """Same layout as Diagnostic.__str__, from the API's JSON form."""
    for d in diags:
        loc = d.get("source") or ""
        if loc and d.get("line"):
            loc += f":{d['line']}"
        msg = f"{d['path']}: {d['message']}" if d.get("path") else d["message"]
        parts = [p for p in (loc, d["severity"], d.get("resource")) if p]
        click.echo("  ".join([*parts, msg]), err=d["severity"] == "error")


def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(str(c))) for w, c in zip(widths, r)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*headers).rstrip()] + [fmt.format(*map(str, r)).rstrip() for r in rows])


# --- preparing documents for the daemon ---------------------------------------


def _document(r: Loaded) -> dict:
    """The raw document, with file-relative paths made absolute for r7sd."""
    data = copy.deepcopy(r.raw)
    spec = data.get("spec") or {}
    if r.kind == "Rig":
        for p in spec.get("plugins") or []:
            if isinstance(p, dict) and p.get("bundle"):
                p["bundle"] = str(resolve_bundle(r, p["bundle"]))
    elif r.kind == "Config":
        if isinstance(spec.get("pluginPaths"), list):
            spec["pluginPaths"] = [
                p if p.startswith(("/", "~")) else str((r.base_dir / p).resolve())
                for p in spec["pluginPaths"]
            ]
        jp = spec.get("jalvPath")
        if isinstance(jp, str) and not jp.startswith(("/", "~")):
            spec["jalvPath"] = str((r.base_dir / jp).resolve())
    return {
        "data": data,
        "source": r.source,
        "lines": [[list(path), line] for path, line in r.lines.items()],
    }


def _load_or_exit(inputs, recursive):
    try:
        result = load(inputs, recursive)
    except InputError as e:
        _err(str(e))
        sys.exit(2)
    if not result.report.ok:
        for d in result.report.sorted():
            click.echo(str(d), err=d.severity == "error")
        sys.exit(1)
    if not result.resources:
        _err("no resources found in input")
        sys.exit(2)
    return result


def register(main: click.Group) -> None:  # noqa: C901 (one place for all commands)

    @main.command()
    @click.option("-f", "--filename", "inputs", multiple=True, required=True, metavar="PATH",
                  help="File, directory, or '-' for stdin (repeatable).")
    @click.option("-R", "--recursive", is_flag=True)
    @click.option("--dry-run", is_flag=True, help="Validate and show what would change; store nothing.")
    @click.option("--wait", is_flag=True, help="Wait until the active rig is Ready.")
    @click.option("--timeout", default=30.0, show_default=True, help="Seconds to --wait.")
    @click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
    @_socket_option
    def apply(inputs, recursive, dry_run, wait, timeout, output, socket):
        """Validate resources (both tiers) and hand them to r7sd."""
        result = _load_or_exit(inputs, recursive)
        client = Client(socket)
        code, resp = _call(client, "POST", "/v1/apply",
                           {"documents": [_document(r) for r in result.resources], "dryRun": dry_run})
        if output == "json":
            click.echo(json.dumps({"status": code, **resp}, indent=2))
            sys.exit(0 if code == 200 else 1)
        _print_diags(resp.get("diagnostics", []))
        if code != 200:
            if "error" in resp:
                _err(resp["error"])
            n = sum(1 for d in resp.get("diagnostics", []) if d["severity"] == "error")
            if n:
                click.echo(f"FAILED: {n} error(s); nothing applied", err=True)
            sys.exit(1)
        suffix = " (dry run)" if dry_run else ""
        for item in resp["results"]:
            click.echo(f"{item['ref']} {item['action']}{suffix}")
        if wait and not dry_run:
            sys.exit(_wait_ready(client, timeout, resp.get("revision", 0)))

    def _wait_ready(client: Client, timeout: float, revision: int = 0) -> int:
        deadline = time.time() + timeout
        last = None
        while True:
            _, st = _call(client, "GET", "/v1/status")
            if st.get("reconciledRevision", -1) < revision:
                if time.time() > deadline:
                    _err("timed out waiting for r7sd to pick up the change")
                    return 1
                time.sleep(0.1)
                continue
            rig = st.get("activeRig")
            phase = (st.get("rigs", {}).get(rig) or {}).get("phase") if rig else None
            if rig is None:
                click.echo("no active rig")
                return 0
            if phase != last:
                click.echo(f"Rig/{rig}: {phase}")
                last = phase
            if phase == "Ready":
                return 0
            if time.time() > deadline:
                _err(f"timed out waiting for Rig/{rig} to be Ready (phase {phase}); see `r7sctl describe rig {rig}`")
                return 1
            time.sleep(0.25)

    def _parse_target(kind: str | None, name: str | None) -> tuple[str | None, str | None]:
        if kind and "/" in kind and name is None:
            kind, name = kind.split("/", 1)
        return kind, name

    @main.command()
    @click.argument("kind", required=False, default="all")
    @click.argument("name", required=False)
    @click.option("-o", "--output", type=click.Choice(["table", "yaml", "json"]), default="table",
                  show_default=True)
    @_socket_option
    def get(kind, name, output, socket):
        """List resources: get rigs | get rig/NAME | get devices | get all."""
        kind, name = _parse_target(kind, name)
        if kind != "all" and canonical_kind(kind) is None:
            _err(f"unknown kind {kind!r}; use rig, device, activerig, config, or all")
            sys.exit(2)
        client = Client(socket)
        if name:
            code, resp = _call(client, "GET", f"/v1/resources/{canonical_kind(kind)}/{name}")
            items = [resp] if code == 200 else []
        else:
            code, resp = _call(client, "GET", f"/v1/resources?kind={kind}")
            items = resp.get("items", [])
        if code != 200:
            _err(resp.get("error", f"HTTP {code}"))
            sys.exit(1)
        if output == "json":
            click.echo(json.dumps(items if not name else items[0], indent=2))
            return
        if output == "yaml":
            click.echo(yaml.safe_dump_all([i["manifest"] for i in items], sort_keys=False), nl=False)
            return
        if not items:
            click.echo("No resources found.")
            return
        by_kind: dict[str, list] = {}
        for i in items:
            by_kind.setdefault(i["kind"], []).append(i)
        blocks = []
        for k, rows in by_kind.items():
            blocks.append(_kind_table(k, rows, prefix=len(by_kind) > 1))
        click.echo("\n\n".join(blocks))

    def _kind_table(kind: str, items: list, prefix: bool) -> str:
        p = f"{kind.lower()}/" if prefix else ""
        if kind == "Rig":
            rows = []
            for i in items:
                st = i.get("status") or {}
                plugins = st.get("plugins") or {}
                conns = st.get("connections") or []
                running = sum(1 for x in plugins.values() if x.get("state") == "Running")
                connected = sum(1 for c in conns if c.get("state") == "Connected")
                spec = i["manifest"].get("spec") or {}
                n_plugins = len(spec.get("plugins") or [])
                n_conns = sum(len((spec.get("connections") or {}).get(s) or []) for s in ("audio", "midi"))
                active = st.get("active", False)
                rows.append([p + i["name"], "yes" if active else "", st.get("phase", "-"),
                             f"{running}/{n_plugins}" if active else str(n_plugins),
                             f"{connected}/{n_conns}" if active else str(n_conns),
                             _age(i.get("appliedAt"))])
            return _table(rows, ["NAME", "ACTIVE", "PHASE", "PLUGINS", "CONNECTIONS", "AGE"])
        if kind == "Device":
            rows = []
            for i in items:
                spec = i["manifest"]["spec"]
                st = i.get("status") or {}
                bus = "usb" if spec.get("usb") else "pci"
                present = "yes" if st.get("present") else ("no" if st else "-")
                rows.append([p + i["name"], bus, spec.get(bus), present,
                             st.get("capture") or "-", st.get("playback") or "-", _age(i.get("appliedAt"))])
            return _table(rows, ["NAME", "BUS", "ID", "PRESENT", "CAPTURE", "PLAYBACK", "AGE"])
        if kind == "ActiveRig":
            return _table([[p + i["name"], i["manifest"]["spec"]["rig"], _age(i.get("appliedAt"))] for i in items],
                          ["NAME", "RIG", "AGE"])
        rows = []
        for i in items:
            spec = i["manifest"].get("spec") or {}
            rows.append([p + i["name"], spec.get("backend", "auto"), spec.get("display", "auto"),
                         ":".join(spec.get("pluginPaths") or []) or "(default)", _age(i.get("appliedAt"))])
        return _table(rows, ["NAME", "BACKEND", "DISPLAY", "PLUGIN PATHS", "AGE"])

    @main.command()
    @click.argument("kind")
    @click.argument("name", required=False)
    @_socket_option
    def describe(kind, name, socket):
        """Show a resource with its live status and recent events."""
        kind, name = _parse_target(kind, name)
        k = canonical_kind(kind or "")
        if k is None or not name:
            _err("usage: r7sctl describe rig NAME | device NAME")
            sys.exit(2)
        code, d = _call(Client(socket), "GET", f"/v1/resources/{k}/{name}")
        if code != 200:
            _err(d.get("error", f"HTTP {code}"))
            sys.exit(1)
        st = d.get("status") or {}
        click.echo(f"Name:        {d['name']}")
        click.echo(f"Kind:        {d['kind']}")
        click.echo(f"Source:      {d.get('source') or '-'}")
        click.echo(f"Generation:  {d.get('generation')}  (applied {_age(d.get('appliedAt'))} ago)")
        if k == "Rig":
            click.echo(f"Active:      {'yes' if st.get('active') else 'no'}")
            click.echo(f"Phase:       {st.get('phase', '-')}")
            for e in st.get("errors") or []:
                click.echo(f"Error:       {e}")
            if st.get("plugins"):
                click.echo("\nPlugins:")
                rows = []
                for pid, p in st["plugins"].items():
                    extra = p.get("lastError") or (f"exit {p['lastExit']}" if p.get("lastExit") is not None else "")
                    if p.get("retryIn"):
                        extra += f", retry in {p['retryIn']}s"
                    rows.append(["  " + pid, p.get("state"), p.get("pid") or "-", p.get("restarts", 0),
                                 p.get("client"), extra.strip(", ")])
                click.echo(_table(rows, ["  ID", "STATE", "PID", "RESTARTS", "JACK CLIENT", "NOTE"]))
            if st.get("connections"):
                click.echo("\nConnections:")
                rows = [["  " + c["from"], c["to"], c["state"], c.get("reason") or ""] for c in st["connections"]]
                click.echo(_table(rows, ["  FROM", "TO", "STATE", "REASON"]))
        elif k == "Device":
            click.echo(f"Present:     {'yes' if st.get('present') else 'no'}")
            if st.get("device"):
                click.echo(f"Hardware:    {st['device']}")
            click.echo(f"Capture:     {st.get('capture') or '-'}")
            click.echo(f"Playback:    {st.get('playback') or '-'}")
            for key, msg in (st.get("errors") or {}).items():
                click.echo(f"Problem:     {key}: {msg}")
        click.echo("\nSpec:")
        click.echo("  " + yaml.safe_dump(d["manifest"].get("spec"), sort_keys=False).replace("\n", "\n  ").rstrip())
        events = d.get("events") or []
        if events:
            click.echo("\nEvents:")
            rows = [["  " + _age(e["time"]), e["reason"] + (f" (x{e['count']})" if e["count"] > 1 else ""),
                     e["object"], e["message"]] for e in events[-20:]]
            click.echo(_table(rows, ["  AGE", "REASON", "OBJECT", "MESSAGE"]))

    @main.command()
    @click.argument("kind", required=False)
    @click.argument("name", required=False)
    @click.option("-f", "--filename", "inputs", multiple=True, metavar="PATH")
    @click.option("-R", "--recursive", is_flag=True)
    @click.option("--dry-run", is_flag=True)
    @_socket_option
    def delete(kind, name, inputs, recursive, dry_run, socket):
        """Delete resources: delete rig NAME | delete -f FILE."""
        kind, name = _parse_target(kind, name)
        refs = []
        if inputs:
            refs = [{"kind": r.kind, "name": r.name} for r in _load_or_exit(inputs, recursive).resources]
        elif kind and name:
            refs = [{"kind": kind, "name": name}]
        else:
            _err("usage: r7sctl delete KIND NAME | delete -f FILE")
            sys.exit(2)
        code, resp = _call(Client(socket), "POST", "/v1/delete", {"refs": refs, "dryRun": dry_run})
        if code != 200:
            _print_diags(resp.get("diagnostics", []))
            _err(resp.get("error", f"HTTP {code}"))
            sys.exit(1)
        for item in resp["results"]:
            click.echo(f"{item['ref']} {item['action']}" + (" (dry run)" if dry_run else ""))

    @main.command()
    @click.option("-f", "--filename", "inputs", multiple=True, required=True, metavar="PATH")
    @click.option("-R", "--recursive", is_flag=True)
    @_socket_option
    def diff(inputs, recursive, socket):
        """Show what `apply` would change. Exit status 1 if anything differs."""
        result = _load_or_exit(inputs, recursive)
        code, resp = _call(Client(socket), "POST", "/v1/diff",
                           {"documents": [_document(r) for r in result.resources]})
        if code != 200:
            _print_diags(resp.get("diagnostics", []))
            sys.exit(2)
        differs = False
        for item in resp["items"]:
            if item["state"] != "unchanged":
                differs = True
                click.echo(item["diff"], nl=False)
        if not differs:
            click.echo("No changes.", err=True)
        sys.exit(1 if differs else 0)

    @main.command()
    @click.argument("rig")
    @click.option("-o", "--output", type=click.Choice(["yaml", "json"]), default="yaml", show_default=True)
    @_socket_option
    def snapshot(rig, output, socket):
        """Print the active Rig with its current live parameter values."""
        code, resp = _call(Client(socket), "GET", f"/v1/snapshot/{rig}")
        if code != 200:
            _err(resp.get("error", f"HTTP {code}"))
            sys.exit(1)
        if output == "json":
            click.echo(json.dumps(resp["manifest"], indent=2))
        else:
            click.echo(resp["yaml"], nl=False)
        if resp["changed"]:
            click.echo(f"# live values differ from the stored rig: {', '.join(resp['changed'])}", err=True)

    @main.command()
    @click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
    @_socket_option
    def status(output, socket):
        """Show daemon, JACK, and backend health."""
        code, st = _call(Client(socket), "GET", "/v1/status")
        if output == "json":
            click.echo(json.dumps(st, indent=2))
            return
        d, j, b = st.get("daemon", {}), st.get("jack", {}), st.get("backend") or {}
        if st.get("starting"):
            click.echo(f"r7sd:      {d.get('version')} (pid {d.get('pid')}): starting up, try again in a moment")
            return
        click.echo(f"r7sd:      {d.get('version')} (pid {d.get('pid')}, up {_age(time.time() - d.get('uptime', 0))})")
        jack = "connected" if j.get("connected") else f"DISCONNECTED: {j.get('error')}"
        if j.get("sampleRate"):
            jack += f" ({j['sampleRate']} Hz, {j['bufferSize']} frames)"
        click.echo(f"JACK:      {jack}")
        hw = st.get("hardware", {}).get("error")
        click.echo(f"Hardware:  {'PipeWire' if not hw else 'UNAVAILABLE: ' + hw}")
        bk = b.get("backend", "-")
        if b.get("path"):
            bk += f" {b.get('version')} at {b['path']}" + (" (patched)" if b.get("patched") else "")
        click.echo(f"Backend:   {bk}")
        for p in st.get("problems") or []:
            click.echo(f"Problem:   {p}")
        rig = st.get("activeRig")
        phase = (st.get("rigs", {}).get(rig) or {}).get("phase") if rig else None
        click.echo(f"Active:    {f'Rig/{rig} ({phase})' if rig else 'none'}")
        click.echo(f"Plugins:   {d.get('plugins')} installed")

    # --- hardware discovery --------------------------------------------------

    @main.group()
    def audio():
        """Discover audio hardware."""

    @audio.command("ports")
    @click.option("-o", "--output", type=click.Choice(["table", "yaml", "json"]), default="table",
                  show_default=True)
    def audio_ports(output):
        """List audio devices and endpoints. -o yaml prints Device resources."""
        try:
            g = hw_snapshot()
        except PipeWireError as e:
            _err(str(e))
            sys.exit(1)
        if output == "json":
            click.echo(json.dumps(_hw_json(g), indent=2))
        elif output == "yaml":
            click.echo(yaml.safe_dump_all(_hw_devices_yaml(g), sort_keys=False), nl=False)
        else:
            rows = []
            for d in g.devices:
                for n in sorted(d.nodes, key=preferred_selector):
                    for direction in ("capture", "playback"):
                        ps = n.ports(direction)
                        if ps:
                            rows.append([d.bus or "-", d.hwid or "-", d.description, direction,
                                         preferred_selector(n), len(ps), ", ".join(p.name for p in ps)])
            if not rows:
                click.echo("No ALSA audio devices visible to PipeWire.")
                return
            click.echo(_table(rows, ["BUS", "ID", "DEVICE", "DIRECTION", "SELECTOR", "CH", "PORTS"]))


def _hw_json(g: HwGraph) -> list[dict]:
    out = []
    for d in g.devices:
        out.append({
            "bus": d.bus, "id": d.hwid, "serial": d.serial, "description": d.description,
            "endpoints": [
                {"direction": direction, "selector": preferred_selector(n), "node": n.name,
                 "description": n.description, "channels": [p.name for p in n.ports(direction)]}
                for n in d.nodes for direction in ("capture", "playback") if n.ports(direction)
            ],
        })
    return out


def _hw_devices_yaml(g: HwGraph) -> list[dict]:
    """Ready-to-apply Device resources: one per device, or one per endpoint
    when a direction is ambiguous."""
    docs = []
    for d in g.devices:
        if not d.bus or not d.hwid:
            continue
        caps, plays = d.endpoints("capture"), d.endpoints("playback")
        base = {"usb" if d.bus == "usb" else "pci": d.hwid}
        if d.bus == "usb" and d.serial and len(g.find(d.bus, d.hwid)) > 1:
            base["serial"] = d.serial
        short = slug(d.nick or d.description)
        if len(caps) <= 1 and len(plays) <= 1:
            docs.append(_device_doc(short, base))
            continue
        for direction, nodes in (("capture", caps), ("playback", plays)):
            for n in sorted(nodes, key=preferred_selector):
                sel = preferred_selector(n)
                docs.append(_device_doc(f"{short}-{slug(sel)}", {**base, direction: sel}))
    return docs


def _device_doc(name: str, spec: dict) -> dict:
    return {"apiVersion": "r7s/v1", "kind": "Device", "metadata": {"name": name[:63].strip("-")}, "spec": spec}

