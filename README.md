# Ravenetes (r7s)

Declare LV2/JACK audio rigs in YAML, Kubernetes-style. See
[docs/DESIGN.md](docs/DESIGN.md) for the full design.

**Status:** phases 1–2 are done:
- plugin discovery and validation
- the `r7sd` daemon, with the jalv backend, hardware resolution through
  PipeWire, and incremental reconcile

Next: MIDI (CC mapping, MIDI hardware), structured logs, and the web UI.

## Setup

r7s needs PipeWire (with `pipewire-jack`) or jackd, and a jalv whose console
`set` command works. Distro jalv 1.8.0 (Ubuntu 26.04) silently ignores it,
so build the patched one:

```bash
sudo apt install meson ninja-build libjack-jackd2-dev liblilv-dev libsuil-dev \
  libserd-dev libsord-dev libsratom-dev libzix-dev lv2-dev
third_party/jalv/build.sh          # installs to ~/.local/opt/r7s-jalv
uv sync
```

To run the daemon as a user service:

```bash
cp packaging/systemd/r7sd.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now r7sd
```

Or run `uv run r7sd` in a terminal.

## Quick start

```bash
uv run r7sctl audio ports -o yaml > devices.yaml        # ready-made Device resources
uv run r7sctl plugins search reverb
uv run r7sctl plugins scaffold urn:zamaudio:ZamVerb --id verb   # paste into spec.plugins
uv run r7sctl validate --live -f rig/                   # offline checks + installed plugins
uv run r7sctl apply -f rig/ --wait                      # hand it to r7sd
uv run r7sctl get rigs
uv run r7sctl describe rig guitar-rig                   # plugins, connections, events
```

Every command takes `-o json`. `apply`, `diff` and `delete` accept files,
directories (`-R`) or `-` for stdin, so remote use is
`ssh pi r7sctl apply -f - < rig.yaml`.

## Plugin search paths

Highest precedence first:

1. `--plugin-path`
2. a `Config` resource
3. `~/.config/r7s/config.yaml`
4. `$LV2_PATH`
5. `~/.lv2`, `/usr/local/lib/lv2`, `/usr/lib/lv2`, and `/usr/lib/<multiarch>/lv2`

## Development

```bash
uv run pytest
```

The tests use fakes for jalv, JACK and PipeWire, plus an LV2 fixture bundle.
They need neither an audio stack nor installed plugins.
