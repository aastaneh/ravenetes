"""Load r7s resources from files, directories, multi-doc streams and stdin."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import yaml
from pydantic import ValidationError

from .diagnostics import Diagnostic, Path, Report
from .schema import KINDS, VALUE_TAGS, Resource

YAML_SUFFIXES = (".yaml", ".yml")


class R7sLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: only true/false, never on/off/yes/no.

    LV2 port symbols such as `on` must stay strings when used as keys.
    """


R7sLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
R7sLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class InputError(Exception):
    """A -f argument that can't be read at all (missing file, etc.)."""


@dataclass
class Loaded:
    resource: Resource
    source: str
    base_dir: FsPath  # for resolving relative paths such as `bundle:`
    lines: dict[Path, int] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.resource.kind

    @property
    def name(self) -> str:
        return self.resource.metadata.name

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}"

    def line_for(self, path: Path) -> int | None:
        """Line of the deepest YAML node on `path` that exists."""
        for n in range(len(path), -1, -1):
            if (line := self.lines.get(tuple(path[:n]))) is not None:
                return line
        return None

    def diag(self, severity, message, path: Path = (), tier: int = 1) -> Diagnostic:
        return Diagnostic(
            severity=severity,
            message=message,
            resource=self.ref,
            path=tuple(path),
            source=self.source,
            line=self.line_for(tuple(path)),
            tier=tier,
        )


@dataclass
class LoadResult:
    resources: list[Loaded] = field(default_factory=list)
    report: Report = field(default_factory=Report)

    def of_kind(self, kind: str) -> list[Loaded]:
        return [r for r in self.resources if r.kind == kind]


def _line_map(node: yaml.Node, path: Path = (), out: dict | None = None) -> dict[Path, int]:
    out = {} if out is None else out
    out.setdefault(path, node.start_mark.line + 1)
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value if isinstance(k, yaml.ScalarNode) else str(k.value)
            # Point a key's path at the key itself, not its value block.
            out[(*path, key)] = k.start_mark.line + 1
            _line_map(v, (*path, key), out)
    elif isinstance(node, yaml.SequenceNode):
        for i, v in enumerate(node.value):
            _line_map(v, (*path, i), out)
    return out


def _clean_loc(loc: tuple) -> Path:
    """Drop discriminator tags pydantic puts into error locations."""
    out: list = []
    for i, seg in enumerate(loc):
        if isinstance(seg, str) and seg in VALUE_TAGS:
            prev2 = loc[i - 2] if i >= 2 else None
            prev1 = loc[i - 1] if i >= 1 else None
            if prev2 == "parameters" or prev1 == "value":
                continue
        out.append(seg)
    return tuple(out)


def _clean_msg(msg: str) -> str:
    return msg.removeprefix("Value error, ").removeprefix("Assertion failed, ")


def _documents(text: str, source: str):
    """Yield (data, line_map, start_line) for each YAML document."""
    loader = R7sLoader(text)
    try:
        while loader.check_node():
            node = loader.get_node()
            data = loader.construct_document(node)
            yield data, _line_map(node), node.start_mark.line + 1
    finally:
        loader.dispose()


def load_text(text: str, source: str, base_dir: FsPath, result: LoadResult) -> None:
    try:
        docs = list(_documents(text, source))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = getattr(e, "problem", None) or str(e)
        result.report.add(
            Diagnostic(
                "error",
                f"YAML syntax error: {problem}",
                source=source,
                line=mark.line + 1 if mark else None,
            )
        )
        return
    for data, lines, start in docs:
        if data is None:
            continue
        if not isinstance(data, dict):
            result.report.add(
                Diagnostic("error", "a resource must be a YAML mapping", source=source, line=start)
            )
            continue
        kind = data.get("kind")
        name = (data.get("metadata") or {}).get("name") if isinstance(data.get("metadata"), dict) else None
        ref = f"{kind}/{name}" if kind and name else kind
        if kind not in KINDS:
            msg = (
                "missing 'kind'"
                if kind is None
                else f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}"
            )
            result.report.add(
                Diagnostic(
                    "error", msg, resource=ref, path=("kind",) if kind else (),
                    source=source, line=lines.get(("kind",), start),
                )
            )
            continue
        try:
            resource = KINDS[kind].model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                path = _clean_loc(err["loc"])
                line = None
                for n in range(len(path), -1, -1):
                    if (line := lines.get(tuple(path[:n]))) is not None:
                        break
                msg = _clean_msg(err["msg"])
                if err["type"] == "extra_forbidden":
                    msg = "unknown field"
                elif err["type"] == "missing":
                    msg = "required field missing"
                result.report.add(
                    Diagnostic("error", msg, resource=ref, path=path, source=source, line=line)
                )
            continue
        result.resources.append(Loaded(resource, source, base_dir, lines))


def expand_inputs(inputs: Iterable[str], recursive: bool = False) -> list[str]:
    """Turn -f arguments into concrete files (or '-' for stdin)."""
    files: list[str] = []
    for arg in inputs:
        if arg == "-":
            files.append("-")
            continue
        p = FsPath(arg)
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            found = sorted(
                f for f in p.glob(pattern) if f.is_file() and f.suffix in YAML_SUFFIXES
            )
            if not found:
                raise InputError(f"{arg}: no .yaml/.yml files" + ("" if recursive else " (use -R to recurse)"))
            files.extend(str(f) for f in found)
        elif p.is_file():
            files.append(str(p))
        else:
            raise InputError(f"{arg}: no such file or directory")
    return files


def load(inputs: Iterable[str], recursive: bool = False, stdin=None) -> LoadResult:
    result = LoadResult()
    for f in expand_inputs(inputs, recursive):
        if f == "-":
            text = (stdin or sys.stdin).read()
            load_text(text, "<stdin>", FsPath.cwd(), result)
        else:
            path = FsPath(f)
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError) as e:
                result.report.add(Diagnostic("error", f"cannot read: {e}", source=f))
                continue
            load_text(text, f, path.resolve().parent, result)
    _check_duplicates(result)
    return result


def _check_duplicates(result: LoadResult) -> None:
    seen: dict[tuple[str, str], Loaded] = {}
    keep: list[Loaded] = []
    for r in result.resources:
        key = (r.kind, r.name)
        if key in seen:
            first = seen[key]
            result.report.add(
                r.diag(
                    "error",
                    f"duplicate {r.ref}; first defined at {first.source}:{first.line_for(())}",
                    ("metadata", "name"),
                )
            )
            continue
        seen[key] = r
        keep.append(r)
    result.resources = keep
