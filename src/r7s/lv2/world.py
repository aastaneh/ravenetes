"""Plugin world: every LV2 plugin discoverable from a set of paths."""

from __future__ import annotations

import difflib
import logging
import os
import platform
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .model import Plugin, Preset
from .ttl import BundleError, default_cache_dir, load_bundle

log = logging.getLogger(__name__)


def _multiarch_dir() -> Path | None:
    machine = platform.machine()
    triplet = {
        "x86_64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
        "armv7l": "arm-linux-gnueabihf",
        "armv6l": "arm-linux-gnueabihf",
        "riscv64": "riscv64-linux-gnu",
    }.get(machine)
    return Path(f"/usr/lib/{triplet}/lv2") if triplet else None


def default_plugin_paths() -> list[str]:
    paths = ["~/.lv2", "/usr/local/lib/lv2", "/usr/lib/lv2"]
    if (m := _multiarch_dir()) is not None:
        paths.append(str(m))
    return paths


def env_plugin_paths() -> list[str] | None:
    value = os.environ.get("LV2_PATH")
    if not value:
        return None
    return [p for p in value.split(":") if p]


def find_bundles(paths: Iterable[str | Path]) -> list[Path]:
    """Every *.lv2 directory with a manifest, in path order, deduplicated."""
    seen: set[Path] = set()
    bundles: list[Path] = []
    for raw in paths:
        root = Path(os.path.expanduser(str(raw)))
        if root.name.endswith(".lv2") and (root / "manifest.ttl").is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(p for p in root.iterdir() if p.name.endswith(".lv2"))
        else:
            continue
        for b in candidates:
            if not (b / "manifest.ttl").is_file():
                continue
            real = b.resolve()
            if real not in seen:
                seen.add(real)
                bundles.append(real)
    return bundles


@dataclass
class PluginWorld:
    paths: list[str]
    plugins: dict[str, Plugin] = field(default_factory=dict)
    presets: dict[str, Preset] = field(default_factory=dict)  # by preset URI
    errors: list[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        paths: Iterable[str | Path],
        extra_bundles: Iterable[str | Path] = (),
        cache_dir: Path | None | bool = True,
        on_miss: Callable[[Path], None] | None = None,
    ) -> PluginWorld:
        """Scan paths. Earlier paths win when two bundles define one URI.

        Extra bundles (rig-local `bundle:` entries) take precedence over
        everything, so a rig can pin its own build of a plugin.
        """
        if cache_dir is True:
            cache_dir = default_cache_dir()
        elif cache_dir is False:
            cache_dir = None
        paths = [str(p) for p in paths]
        world = cls(paths=paths)
        for bundle in find_bundles([*extra_bundles, *paths]):
            try:
                scan = load_bundle(bundle, cache_dir, on_miss)
            except BundleError as e:
                world.errors.append(str(e))
                log.warning("%s", e)
                continue
            for p in scan.plugins:
                world.plugins.setdefault(p.uri, p)
            for pr in scan.presets:
                world.presets.setdefault(pr.uri, pr)
        return world

    def get(self, uri: str) -> Plugin | None:
        return self.plugins.get(uri)

    def all(self, category: str | None = None) -> list[Plugin]:
        result = sorted(self.plugins.values(), key=lambda p: (p.name.lower(), p.uri))
        if category:
            cat = category.lower()
            result = [p for p in result if any(c.lower() == cat for c in p.classes)]
        return result

    def categories(self) -> list[str]:
        return sorted({c for p in self.plugins.values() for c in p.classes})

    def search(self, query: str, category: str | None = None) -> list[Plugin]:
        """Rank plugins by how well name/URI match the query.

        Substring hits on the name rank first, then on the URI, then fuzzy
        matches on the name. Every query token has to hit somewhere.
        """
        q = query.lower().strip()
        tokens = q.split()
        scored: list[tuple[float, Plugin]] = []
        for p in self.all(category):
            name, uri = p.name.lower(), p.uri.lower()
            hay = f"{name} {uri} {' '.join(p.classes).lower()}"
            if tokens and all(t in hay for t in tokens):
                score = 3.0 if q in name else 2.0 if all(t in name for t in tokens) else 1.0
                scored.append((score + 1 / (1 + len(name)), p))
                continue
            ratio = max(
                (difflib.SequenceMatcher(None, q, w).ratio() for w in name.split()),
                default=0.0,
            )
            if ratio >= 0.75:
                scored.append((ratio * 0.9, p))
        scored.sort(key=lambda s: (-s[0], s[1].name.lower()))
        return [p for _, p in scored]

    def presets_for(self, plugin_uri: str) -> list[Preset]:
        out = [p for p in self.presets.values() if p.applies_to == plugin_uri]
        # Labelled presets first: they're the ones people pick by name.
        return sorted(out, key=lambda p: (p.label is None, (p.label or "").lower(), p.uri))

    def find_preset(self, plugin_uri: str, ref: str) -> tuple[Preset | None, str | None]:
        """A preset of this plugin by URI or label: (preset, None) or (None, why)."""
        mine = self.presets_for(plugin_uri)
        hit = self.presets.get(ref)
        if hit is not None:
            if hit.applies_to != plugin_uri:
                return None, f"preset {ref!r} belongs to {hit.applies_to}, not {plugin_uri}"
            return hit, None
        for match in (lambda p: p.label == ref, lambda p: (p.label or "").casefold() == ref.casefold()):
            found = [p for p in mine if match(p)]
            if len(found) == 1:
                return found[0], None
            if len(found) > 1:
                uris = ", ".join(p.uri for p in found)
                return None, f"{len(found)} presets are labelled {ref!r}; use the URI: {uris}"
        if not mine:
            return None, f"no presets found for {plugin_uri} in the plugin paths"
        names = ", ".join(repr(p.label or p.uri) for p in mine[:10])
        close = difflib.get_close_matches(ref, [p.label for p in mine if p.label], n=1, cutoff=0.6)
        hint = f"; did you mean {close[0]!r}?" if close else f"; available: {names}"
        return None, f"no preset {ref!r} for this plugin{hint}"

    def suggest(self, uri: str, n: int = 3) -> list[str]:
        return difflib.get_close_matches(uri, list(self.plugins), n=n, cutoff=0.6)
