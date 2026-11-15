"""Pure-Python LV2 bundle discovery by parsing Turtle metadata with rdflib.

Mirrors what lilv does for the subset r7s needs: read each bundle's
manifest.ttl, find lv2:Plugin subjects, load only the rdfs:seeAlso files of
those plugins (skipping presets and UI descriptions), and extract ports.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef

from .model import Plugin, Port, PortType, ScalePoint

log = logging.getLogger(__name__)

LV2 = Namespace("http://lv2plug.in/ns/lv2core#")
ATOM = Namespace("http://lv2plug.in/ns/ext/atom#")
EV = Namespace("http://lv2plug.in/ns/ext/event#")
MIDI = Namespace("http://lv2plug.in/ns/ext/midi#")
UNITS = Namespace("http://lv2plug.in/ns/extensions/units#")
UI = Namespace("http://lv2plug.in/ns/extensions/ui#")
DOAP = Namespace("http://usefulinc.com/ns/doap#")
PPROPS = Namespace("http://lv2plug.in/ns/ext/port-props#")

CACHE_VERSION = 1

UNIT_LABELS = {
    "db": "dB",
    "pc": "%",
    "hz": "Hz",
    "khz": "kHz",
    "mhz": "MHz",
    "ms": "ms",
    "s": "s",
    "min": "min",
    "bpm": "BPM",
    "beat": "beats",
    "bar": "bars",
    "semitone12TET": "semitones",
    "cent": "cents",
    "oct": "octaves",
    "degree": "°",
    "coef": "coef",
    "frame": "frames",
    "m": "m",
    "cm": "cm",
    "mm": "mm",
    "km": "km",
    "inch": "in",
    "mile": "mi",
    "midiNote": "MIDI note",
}


class BundleError(Exception):
    pass


def _file_path(uri: URIRef | str) -> Path | None:
    parsed = urlparse(str(uri))
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def _num(value) -> float | None:
    if isinstance(value, Literal):
        try:
            return float(value.toPython())
        except (TypeError, ValueError):
            return None
    return None


def _local_name(uri: URIRef | str) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[1]
    return s


def _port_type(g: Graph, port) -> PortType:
    types = set(g.objects(port, RDF.type))
    if LV2.AudioPort in types:
        return "audio"
    if LV2.ControlPort in types:
        return "control"
    if LV2.CVPort in types:
        return "cv"
    if ATOM.AtomPort in types:
        if MIDI.MidiEvent in set(g.objects(port, ATOM.supports)):
            return "midi"
        return "atom"
    if EV.EventPort in types:
        if MIDI.MidiEvent in set(g.objects(port, EV.supportsEvent)):
            return "midi"
        return "atom"
    return "unknown"


def _unit(g: Graph, port) -> str | None:
    unit = g.value(port, UNITS.unit)
    if unit is None:
        return None
    if isinstance(unit, URIRef) and str(unit).startswith(str(UNITS)):
        name = _local_name(unit)
        return UNIT_LABELS.get(name, name)
    sym = g.value(unit, UNITS.symbol) or g.value(unit, RDFS.label)
    return str(sym) if sym is not None else _local_name(unit)


def _parse_port(g: Graph, node) -> Port | None:
    index = g.value(node, LV2["index"])
    symbol = g.value(node, LV2.symbol)
    if index is None or symbol is None:
        return None
    types = set(g.objects(node, RDF.type))
    direction = "output" if LV2.OutputPort in types else "input"
    props = sorted(
        {_local_name(p) for p in g.objects(node, LV2.portProperty) if isinstance(p, URIRef)}
    )
    scale_points = []
    for sp in g.objects(node, LV2.scalePoint):
        label = g.value(sp, RDFS.label)
        value = _num(g.value(sp, RDF.value))
        if label is not None and value is not None:
            scale_points.append(ScalePoint(label=str(label), value=value))
    scale_points.sort(key=lambda s: s.value)
    name = g.value(node, LV2.name)
    return Port(
        index=int(index.toPython()),
        symbol=str(symbol),
        name=str(name) if name is not None else str(symbol),
        direction=direction,
        type=_port_type(g, node),
        default=_num(g.value(node, LV2.default)),
        minimum=_num(g.value(node, LV2.minimum)),
        maximum=_num(g.value(node, LV2.maximum)),
        unit=_unit(g, node),
        properties=props,
        scale_points=scale_points,
    )


def _plugin_name(g: Graph, uri: URIRef) -> str:
    for pred in (DOAP.name, RDFS.label, LV2.name):
        names = list(g.objects(uri, pred))
        if names:
            # Prefer an untagged or English literal.
            for n in names:
                if isinstance(n, Literal) and n.language in (None, "en"):
                    return str(n)
            return str(names[0])
    return _local_name(uri)


def _bundle_files(bundle: Path) -> list[Path]:
    return sorted(bundle.glob("*.ttl"))


def bundle_fingerprint(bundle: Path) -> str:
    h = hashlib.sha256(f"v{CACHE_VERSION}:{bundle.resolve()}".encode())
    for f in _bundle_files(bundle):
        st = f.stat()
        h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:32]


def parse_bundle(bundle: Path) -> list[Plugin]:
    """Parse one *.lv2 bundle directory into Plugin records."""
    bundle = bundle.resolve()
    manifest = bundle / "manifest.ttl"
    if not manifest.is_file():
        raise BundleError(f"{bundle}: no manifest.ttl")
    g = Graph()
    try:
        g.parse(manifest, format="turtle")
    except Exception as e:  # rdflib raises a zoo of exception types
        raise BundleError(f"{manifest}: {e}") from e

    plugin_uris = [s for s in g.subjects(RDF.type, LV2.Plugin) if isinstance(s, URIRef)]
    loaded: set[Path] = {manifest}
    for uri in plugin_uris:
        for see in g.objects(uri, RDFS.seeAlso):
            path = _file_path(see)
            if path is None or path in loaded or not path.is_file():
                continue
            loaded.add(path)
            try:
                g.parse(path, format="turtle")
            except Exception as e:
                log.warning("skipping %s: %s", path, e)

    plugins = []
    for uri in plugin_uris:
        binary = g.value(uri, LV2.binary)
        bin_path = _file_path(binary) if binary is not None else None
        classes = sorted(
            {
                _local_name(c).removesuffix("Plugin")
                for c in g.objects(uri, RDF.type)
                if isinstance(c, URIRef)
                and str(c).startswith(str(LV2))
                and c != LV2.Plugin
                and str(c).endswith("Plugin")
            }
        )
        ui_types = sorted(
            {
                _local_name(t)
                for u in g.objects(uri, UI.ui)
                for t in g.objects(u, RDF.type)
                if isinstance(t, URIRef) and str(t).startswith(str(UI))
            }
        )
        ports = [p for node in g.objects(uri, LV2.port) if (p := _parse_port(g, node))]
        ports.sort(key=lambda p: p.index)
        plugins.append(
            Plugin(
                uri=str(uri),
                name=_plugin_name(g, uri),
                bundle=str(bundle),
                binary=str(bin_path) if bin_path else None,
                classes=classes,
                ui_types=ui_types,
                ports=ports,
            )
        )
    return plugins


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "r7s" / "lv2"


def load_bundle(
    bundle: Path, cache_dir: Path | None = None, on_miss: Callable[[Path], None] | None = None
) -> list[Plugin]:
    """parse_bundle with an on-disk JSON cache keyed by TTL mtimes."""
    if cache_dir is None:
        return parse_bundle(bundle)
    key = bundle_fingerprint(bundle)
    cache_file = cache_dir / f"{key}.json"
    try:
        data = json.loads(cache_file.read_text())
        return [Plugin.from_dict(d) for d in data]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if on_miss is not None:
        on_miss(bundle)
    plugins = parse_bundle(bundle)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps([p.to_dict() for p in plugins]))
        tmp.replace(cache_file)
    except OSError as e:
        log.debug("could not write cache %s: %s", cache_file, e)
    return plugins
