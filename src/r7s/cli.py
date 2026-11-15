"""r7sctl command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import yaml

from . import __version__
from .config import resolve_plugin_paths
from .diagnostics import Report
from .loader import InputError, load
from .lv2.model import Plugin
from .lv2.world import PluginWorld
from .scaffold import scaffold_dict, scaffold_yaml
from .validate import rig_bundles, validate_environment, validate_structure


def _err(msg: str) -> None:
    click.echo(f"r7sctl: {msg}", err=True)


def _plugin_path_option(f):
    f = click.option(
        "--no-cache", is_flag=True, help="Re-parse LV2 metadata instead of using ~/.cache/r7s."
    )(f)
    return click.option(
        "--plugin-path",
        "plugin_paths",
        multiple=True,
        metavar="DIR",
        help="LV2 search path (repeatable). Overrides Config and $LV2_PATH.",
    )(f)


def _on_miss(bundle: Path) -> None:
    if sys.stderr.isatty():
        click.echo(f"r7sctl: indexing {bundle} (cached after first run)…", err=True)


def _world(plugin_paths, no_cache: bool, inputs=None, extra_bundles=()) -> PluginWorld:
    resolved = resolve_plugin_paths(plugin_paths, inputs)
    world = PluginWorld.load(
        resolved.paths, extra_bundles=extra_bundles, cache_dir=not no_cache, on_miss=_on_miss
    )
    for e in world.errors:
        _err(f"warning: {e}")
    return world


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers).rstrip()]
    lines += [fmt.format(*r).rstrip() for r in rows]
    return "\n".join(lines)


def _summary(p: Plugin) -> dict:
    io = lambda t, d: len(p.ports_of(t, d))  # noqa: E731
    return {
        "uri": p.uri,
        "name": p.name,
        "category": p.category,
        "classes": p.classes,
        "audio_in": io("audio", "input"),
        "audio_out": io("audio", "output"),
        "midi_in": io("midi", "input"),
        "midi_out": io("midi", "output"),
        "controls": sum(1 for q in p.ports if q.is_control_input),
        "has_ui": p.has_ui,
        "bundle": p.bundle,
    }


def _print_plugins(plugins: list[Plugin], output: str) -> None:
    if output == "json":
        click.echo(json.dumps([_summary(p) for p in plugins], indent=2))
    elif output == "yaml":
        click.echo(yaml.safe_dump([_summary(p) for p in plugins], sort_keys=False), nl=False)
    elif output == "uri":
        for p in plugins:
            click.echo(p.uri)
    else:
        rows = []
        for p in plugins:
            s = _summary(p)
            rows.append([
                p.name,
                p.category or "-",
                f"{s['audio_in']}/{s['audio_out']}",
                f"{s['midi_in']}/{s['midi_out']}",
                p.uri,
            ])
        click.echo(_table(rows, ["NAME", "CATEGORY", "AUDIO", "MIDI", "URI"]))


OUTPUT_LIST = click.Choice(["table", "json", "yaml", "uri"])


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="r7sctl")
def main() -> None:
    """Ravenetes: declare LV2/JACK audio rigs in YAML."""


# --- plugins -------------------------------------------------------------


@main.group()
def plugins() -> None:
    """Discover installed LV2 plugins."""


@plugins.command("list")
@click.option("--category", help="Only plugins of this LV2 class, e.g. Reverb.")
@click.option("-o", "--output", type=OUTPUT_LIST, default="table", show_default=True)
@_plugin_path_option
def plugins_list(category, output, plugin_paths, no_cache) -> None:
    """List installed plugins."""
    world = _world(plugin_paths, no_cache)
    found = world.all(category)
    if category and not found:
        _err(f"no plugins in category {category!r}; categories: {', '.join(world.categories())}")
        sys.exit(1)
    _print_plugins(found, output)


@plugins.command("categories")
@click.option("-o", "--output", type=click.Choice(["table", "json"]), default="table", show_default=True)
@_plugin_path_option
def plugins_categories(output, plugin_paths, no_cache) -> None:
    """List LV2 categories with plugin counts."""
    world = _world(plugin_paths, no_cache)
    counts = {c: len(world.all(c)) for c in world.categories()}
    if output == "json":
        click.echo(json.dumps(counts, indent=2))
    else:
        click.echo(_table([[c, str(n)] for c, n in counts.items()], ["CATEGORY", "PLUGINS"]))


@plugins.command("search")
@click.argument("query")
@click.option("--category", help="Only plugins of this LV2 class.")
@click.option("-o", "--output", type=OUTPUT_LIST, default="table", show_default=True)
@_plugin_path_option
def plugins_search(query, category, output, plugin_paths, no_cache) -> None:
    """Fuzzy-search plugins by name, URI, or category."""
    world = _world(plugin_paths, no_cache)
    found = world.search(query, category)
    if not found:
        _err(f"no plugins match {query!r}")
        sys.exit(1)
    _print_plugins(found, output)


def _lookup(world: PluginWorld, uri: str) -> Plugin:
    plugin = world.get(uri)
    if plugin is not None:
        return plugin
    hits = world.search(uri)
    if len(hits) == 1:
        return hits[0]
    msg = f"plugin {uri!r} not found"
    if hits:
        msg += "; matches:\n" + "\n".join(f"  {p.uri}  ({p.name})" for p in hits[:8])
    elif sugg := world.suggest(uri):
        msg += f"; did you mean {sugg[0]!r}?"
    _err(msg)
    sys.exit(1)


@plugins.command("inspect")
@click.argument("uri")
@click.option("-o", "--output", type=click.Choice(["text", "json", "yaml"]), default="text", show_default=True)
@_plugin_path_option
def plugins_inspect(uri, output, plugin_paths, no_cache) -> None:
    """Show every port of a plugin. Accepts a URI or an unambiguous search."""
    plugin = _lookup(_world(plugin_paths, no_cache), uri)
    if output == "json":
        click.echo(json.dumps(plugin.to_dict(), indent=2))
        return
    if output == "yaml":
        click.echo(yaml.safe_dump(plugin.to_dict(), sort_keys=False), nl=False)
        return
    click.echo(f"Name:      {plugin.name}")
    click.echo(f"URI:       {plugin.uri}")
    click.echo(f"Category:  {', '.join(plugin.classes) or '-'}")
    click.echo(f"Bundle:    {plugin.bundle}")
    click.echo(f"Native UI: {', '.join(plugin.ui_types) or 'none'}")
    for type_, direction, title in (
        ("audio", "input", "Audio inputs"),
        ("audio", "output", "Audio outputs"),
        ("cv", "input", "CV inputs"),
        ("cv", "output", "CV outputs"),
        ("midi", "input", "MIDI inputs"),
        ("midi", "output", "MIDI outputs"),
    ):
        ports = plugin.ports_of(type_, direction)
        if ports:
            click.echo(f"\n{title}:")
            for p in ports:
                click.echo(f"  {p.symbol}" + (f"  ({p.name})" if p.name != p.symbol else ""))
    for direction, title in (("input", "Parameters"), ("output", "Monitors (read-only)")):
        ports = plugin.ports_of("control", direction)
        if not ports:
            continue
        click.echo(f"\n{title}:")
        rows = []
        for p in ports:
            fmt = lambda x: "-" if x is None else f"{x:g}"  # noqa: E731
            flags = [f for f in p.properties if f in ("toggled", "integer", "enumeration", "logarithmic")]
            if p.scale_points:
                flags.append("values: " + "|".join(s.label for s in p.scale_points))
            rows.append([p.symbol, p.name, fmt(p.default), fmt(p.minimum), fmt(p.maximum), p.unit or "", " ".join(flags)])
        click.echo(_table(rows, ["SYMBOL", "NAME", "DEFAULT", "MIN", "MAX", "UNIT", "FLAGS"]))


@plugins.command("scaffold")
@click.argument("uri")
@click.option("--id", "plugin_id", help="Plugin id to use in the stanza (default: from the name).")
@click.option("-o", "--output", type=click.Choice(["yaml", "json"]), default="yaml", show_default=True)
@_plugin_path_option
def plugins_scaffold(uri, plugin_id, output, plugin_paths, no_cache) -> None:
    """Print a ready-to-paste spec.plugins entry with defaults and ranges."""
    plugin = _lookup(_world(plugin_paths, no_cache), uri)
    if output == "json":
        click.echo(json.dumps(scaffold_dict(plugin, plugin_id), indent=2))
    else:
        click.echo(scaffold_yaml(plugin, plugin_id), nl=False)


# --- validate ------------------------------------------------------------


@main.command()
@click.option("-f", "--filename", "inputs", multiple=True, required=True, metavar="PATH",
              help="File, directory, or '-' for stdin (repeatable).")
@click.option("-R", "--recursive", is_flag=True, help="Recurse into directories given with -f.")
@click.option("--live", is_flag=True, help="Also check against installed plugins (tier 2).")
@click.option("--strict", is_flag=True, help="Treat unresolved references as errors.")
@click.option("-o", "--output", type=click.Choice(["text", "json"]), default="text", show_default=True)
@_plugin_path_option
def validate(inputs, recursive, live, strict, output, plugin_paths, no_cache) -> None:
    """Validate resources. Exit status 1 if any errors."""
    try:
        result = load(inputs, recursive)
    except InputError as e:
        _err(str(e))
        sys.exit(2)
    report = Report(list(result.report.diagnostics))
    report.extend(validate_structure(result, strict_refs=strict))
    if live:
        world = _world(plugin_paths, no_cache, result, rig_bundles(result))
        report.extend(validate_environment(result, world))

    errors, warnings = report.count("error"), report.count("warning")
    if output == "json":
        click.echo(json.dumps({
            "valid": report.ok,
            "tiers": [1, 2] if live else [1],
            "resources": [r.ref for r in result.resources],
            "errors": errors,
            "warnings": warnings,
            "diagnostics": [d.to_dict() for d in report.sorted()],
        }, indent=2))
    else:
        for d in report.sorted():
            click.echo(str(d), err=d.severity == "error")
        tiers = "tiers 1+2" if live else "tier 1"
        n = len(result.resources)
        if report.ok:
            tail = f", {warnings} warning(s)" if warnings else ""
            click.echo(f"OK: {n} resource(s) valid ({tiers}){tail}")
        else:
            click.echo(f"FAILED: {errors} error(s), {warnings} warning(s) in {n} resource(s) ({tiers})", err=True)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
