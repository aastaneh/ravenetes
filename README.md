# Ravenetes (r7s)

Declare LV2/JACK audio rigs in YAML, Kubernetes-style. See
[docs/DESIGN.md](docs/DESIGN.md) for the full design.

**Status:** phase 1. Plugin discovery and validation work. The daemon,
`apply`, and the audio backends come next.

## Quick start

```bash
uv sync
uv run r7sctl plugins search reverb
uv run r7sctl plugins inspect urn:zamaudio:ZamVerb
uv run r7sctl plugins scaffold urn:zamaudio:ZamVerb --id verb   # paste into spec.plugins
uv run r7sctl validate -f examples/rig                # tier 1: structure
uv run r7sctl validate --live -f examples/rig         # tier 2: plus installed plugins
```

Every command takes `-o json`.

The first run indexes the LV2 bundles (about 15s for large bundles like
lsp-plugins). After that, runs read from `~/.cache/r7s/lv2/`.

## Plugin search paths

Highest precedence first:

1. `--plugin-path`
2. a `Config` resource passed with `-f`
3. `~/.config/r7s/config.yaml`
4. `$LV2_PATH`
5. `~/.lv2`, `/usr/local/lib/lv2`, `/usr/lib/lv2`, and `/usr/lib/<multiarch>/lv2`

## Development

```bash
uv run pytest
```

The tests use their own LV2 fixture bundle in `tests/fixtures/lv2`. They
don't depend on which plugins are installed.
