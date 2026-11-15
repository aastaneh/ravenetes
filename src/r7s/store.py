"""Durable desired state: one JSON file per resource under the state dir."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .loader import Loaded, LoadResult, load_document

log = logging.getLogger(__name__)


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "r7s"


def _spec_key(manifest: dict) -> str:
    """What counts as a change: spec plus labels (not source or comments)."""
    meta = manifest.get("metadata") or {}
    return json.dumps({"spec": manifest.get("spec"), "labels": meta.get("labels")}, sort_keys=True)


@dataclass
class Stored:
    kind: str
    name: str
    manifest: dict
    source: str
    generation: int
    applied_at: float

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "manifest": self.manifest,
            "source": self.source,
            "generation": self.generation,
            "appliedAt": self.applied_at,
        }


class Store:
    def __init__(self, state_dir: Path | None = None):
        self.dir = (state_dir or default_state_dir()) / "resources"
        self.lock = threading.RLock()
        self._items: dict[tuple[str, str], Stored] = {}
        self.revision = 0  # bumps on every change; the reconciler watches it

    def load(self) -> list[str]:
        """Read the store from disk. Returns problems (corrupt files are skipped)."""
        problems = []
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._items.clear()
            for f in sorted(self.dir.glob("*/*.json")):
                try:
                    d = json.loads(f.read_text())
                    s = Stored(d["kind"], d["name"], d["manifest"], d.get("source", ""),
                               int(d.get("generation", 1)), float(d.get("appliedAt", 0)))
                except (OSError, ValueError, KeyError, TypeError) as e:
                    problems.append(f"skipping corrupt state file {f}: {e}")
                    continue
                self._items[(s.kind, s.name)] = s
            self.revision += 1
        return problems

    def _path(self, kind: str, name: str) -> Path:
        return self.dir / kind / f"{name}.json"

    def get(self, kind: str, name: str) -> Stored | None:
        with self.lock:
            return self._items.get((kind, name))

    def list(self, kind: str | None = None) -> list[Stored]:
        with self.lock:
            items = [s for s in self._items.values() if kind is None or s.kind == kind]
        return sorted(items, key=lambda s: (s.kind, s.name))

    def put(self, kind: str, name: str, manifest: dict, source: str) -> tuple[Stored, str]:
        """Create or update. Returns (stored, 'created'|'configured'|'unchanged')."""
        with self.lock:
            old = self._items.get((kind, name))
            if old is not None and _spec_key(old.manifest) == _spec_key(manifest):
                return old, "unchanged"
            s = Stored(kind, name, manifest, source, (old.generation + 1) if old else 1, time.time())
            path = self._path(kind, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s.to_dict(), indent=2))
            tmp.replace(path)
            self._items[(kind, name)] = s
            self.revision += 1
            return s, "configured" if old else "created"

    def delete(self, kind: str, name: str) -> bool:
        with self.lock:
            if self._items.pop((kind, name), None) is None:
                return False
            try:
                self._path(kind, name).unlink()
            except FileNotFoundError:
                pass
            self.revision += 1
            return True

    def as_load_result(self, exclude: set[tuple[str, str]] = frozenset()) -> LoadResult:
        """Stored resources as validated Loaded objects (for admission/reconcile)."""
        result = LoadResult()
        for s in self.list():
            if (s.kind, s.name) in exclude:
                continue
            loaded = load_document(s.manifest, s.source or f"<store:{s.ref}>", Path("/"), {}, result)
            if loaded is None:
                log.error("stored %s no longer validates: %s", s.ref, result.report.diagnostics[-1])
        return result


def merged(base: LoadResult, incoming: list[Loaded]) -> LoadResult:
    """`base` with resources replaced/added by `incoming` (same kind/name wins)."""
    keys = {(r.kind, r.name) for r in incoming}
    out = LoadResult()
    out.resources = [r for r in base.resources if (r.kind, r.name) not in keys] + list(incoming)
    return out
