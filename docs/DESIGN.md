# Ravenetes (r7s)

YAML-driven LV2/JACK audio plugin host: a Kubernetes-inspired replacement for
the MOD/modep graphical patchbay.

## Status

Design settled (2026-11-15). Phases 1 (schema, validation, plugin discovery)
and 2 (daemon, jalv backend, hardware resolution, reconciler) implemented.

## Motivation

- MOD/modep only runs on ARM32 (Blokas Pi hardware).
- A graphical patchbay is fragile and can't be version-controlled.
- YAML as the source of truth is git-native, reproducible and scriptable.

## Core concept

You declare your audio rig in YAML. A reconciler daemon applies it to a live
JACK/PipeWire + LV2 stack, and a read-only web UI renders the signal graph.
That is all r7s does.

## Architecture

```
rig YAML (files, directories, stdin)
      │  r7sctl apply / validate / diff / get / delete
      ▼
r7sd API  (HTTP over Unix socket: $XDG_RUNTIME_DIR/r7s.sock)
      │  admission: tier 1 (structural) + tier 2 (environmental)
      ▼
state store  (~/.local/state/r7s/: desired resources, last-applied)
      │
      ▼
reconciler  (diff desired vs. observed → minimal action plan)
      │
      ▼
backend: jalv (v1) | mod-host (later) | carla (later, VST3/CLAP)
      │
      ▼
JACK API (PipeWire's JACK via pw-jack, or jackd)
      │
      ▼
audio + MIDI hardware
```

- `r7sctl`: the CLI. Discovery and offline validation run locally. Commands
  that change state talk to `r7sd`.
- `r7sd`: the daemon, run as a systemd user unit. It owns the state store,
  the reconciler, the backend processes, its own JACK client (for connection
  management and MIDI CC mapping), the event log, and the web UI.
- Remote use goes over ssh: `ssh pi r7sctl apply -f - < rig.yaml`.

## Resources

All resources share the Kubernetes envelope:

```yaml
apiVersion: r7s/v1
kind: <Kind>
metadata:
  name: <dns-label-ish name>
spec: {...}
```

Names are unique per kind. A reference resolves against everything applied,
not just the current file.

### Device

JACK/PipeWire port names are an implementation detail that r7s owns. Users
refer to hardware by stable bus IDs, and r7s resolves them to current port
names at apply time. It re-resolves on daemon startup and on udev events.

```yaml
apiVersion: r7s/v1
kind: Device
metadata:
  name: scarlett
spec:
  usb: "1235:8210"        # vendor:product from lsusb
  serial: "Y7XXXXXX"      # optional: tells identical units apart
---
apiVersion: r7s/v1
kind: Device
metadata:
  name: builtin
spec:
  pci: "8086:a171"        # from lspci
```

Exactly one of `usb` or `pci` is required.

**Endpoint selectors.** One card can expose several PipeWire endpoints. A
laptop's HDA controller, for example, has Speaker, HDMI1–3, Mic1 and Mic2.
`capture:` and `playback:` pick one per direction:

```yaml
spec:
  pci: "8086:51c8"
  capture: Mic2           # UCM endpoint name, node nick, or description
  playback: Speaker
```

- A direction with exactly one endpoint needs no selector, which covers most
  USB interfaces.
- If a direction is ambiguous, its edges stay Unresolved and the reason lists
  the choices. `apply` warns about it only when some rig uses that side.
- Two endpoints of one card mean two Devices (e.g. `laptop-mic`,
  `laptop-speaker`).
- `r7sctl audio ports -o yaml` prints ready-made Devices with selectors.
- Selectors match exactly first (case-insensitive), then by unique substring.

**Resolution is PipeWire-only in v1.** r7s matches the PipeWire Device object
on `device.bus` + `device.vendor.id`/`device.product.id` (+ `device.serial`;
the bare serial or udev's `Vendor_Model_SERIAL` form both match), then the
node, then its physical non-monitor ports in `port.id` order. The JACK name
is `<node.description>:<port.name>`. Under plain jackd, device edges are
Unresolved, while plugins and plugin-to-plugin edges still work. MIDI
hardware resolution lands in phase 3.

**Canonical device port names** (r7s maps these to real ports):

| Name | Type | Direction |
|---|---|---|
| `capture_<n>` (1-based); `capture_l` = 1, `capture_r` = 2 | audio | source |
| `playback_<n>`; `playback_l` = 1, `playback_r` = 2 | audio | sink |
| `midi_capture`, `midi_capture_<n>` | midi | source |
| `midi_playback`, `midi_playback_<n>` | midi | sink |

You can reference hardware inline without an alias: `usb/1235:8210/capture_l`
or `pci/8086:a171/playback_1`.

### Rig

The primary resource: a plugin graph plus its connections.

```yaml
apiVersion: r7s/v1
kind: Rig
metadata:
  name: guitar-rig
spec:
  plugins:
    - id: amp
      lv2: "http://guitarix.sourceforge.net/plugins/gx_amp#GUITARIX"
      parameters:
        DRIVE: 0.7                          # scalar shorthand
        BASS: { value: 0.5, cc: 11, channel: 1, device: midimix }
        TREBLE: { value: 0.4, cc: 12, device: midimix, min: 0.2, max: 0.8 }
        MODE: "Lead"                        # enumeration label (lv2:scalePoint)
    - id: verb
      lv2: "urn:zamaudio:ZamVerb"
      bundle: ./plugins/ZamVerb.lv2         # optional, relative to this file
      ui: false                             # opt out of the native UI window
  connections:
    audio:
      - from: scarlett:capture_l
        to: amp:in
      - from: amp:out
        to: verb:lv2_audio_in_1
      - from: verb:lv2_audio_out_1
        to: scarlett:playback_l
    midi:
      - from: midimix:midi_capture
        to: amp:midi_in
```

- **Plugin format is tagged.** Each plugin sets exactly one of `lv2:`,
  `vst3:` or `clap:`. Only `lv2:` runs in v1. The others are reserved in the
  schema and rejected by validation until a Carla backend exists.
- **Full IDs only.** No aliases or fuzzy names. `r7sctl plugins scaffold`
  generates the stanza for you.
- **`bundle:`** adds one local LV2 bundle to discovery for this plugin, so a
  rig repo can ship its own plugins. It is resolved relative to the file that
  declares it.
- **Parameters** are addressed by LV2 port symbol and given in the plugin's
  own units. A value is a number, a bool (for toggled ports), or a scale-point
  label (for enumerations).
- **MIDI CC mapping** goes inline on the parameter: `cc` (0–127), `channel`
  (1–16, omitted = omni), `device` (Device alias), and optional `min`/`max`,
  which default to the port's range. `r7sd` does the mapping itself through
  its own JACK MIDI client, since jalv has no CC mapping.
- **Endpoints** are `<plugin-id | device-alias>:<port>` or an inline hardware
  reference. Plugin ids must not collide with device aliases. Ids are at
  most 48 characters, because the JACK client is `r7s-<id>` and JACK caps
  names at 63.
- **Port-level edges.** Each port of a stereo plugin is wired explicitly.

### ActiveRig

Selects which rig is live. It is a singleton (by convention named `current`).

```yaml
apiVersion: r7s/v1
kind: ActiveRig
metadata:
  name: current
spec:
  rig: guitar-rig
```

### Config

Also a singleton. `metadata` is optional and defaults to `name: default`.

```yaml
apiVersion: r7s/v1
kind: Config
spec:
  backend: auto          # auto | jalv | mod-host
  display: auto          # auto | always | never
  pluginPaths:
    - ~/.lv2
    - /usr/local/lib/lv2
    - /usr/lib/lv2
```

**Plugin path precedence:**

1. `--plugin-path` flags.
2. A Config resource among the `-f` inputs.
3. `~/.config/r7s/config.yaml`.
4. `$LV2_PATH`.
5. Built-in defaults: `~/.lv2`, `/usr/local/lib/lv2`, `/usr/lib/lv2`, and
   the multiarch `/usr/lib/<triplet>/lv2`.

## Inputs

`-f` accepts the same things kubectl does:

- a file
- a multi-document stream (`---`)
- a directory (`-R` to recurse)
- `-` for stdin
- repeated `-f` flags

## Apply semantics

- `apply` creates or updates resources by `(kind, name)`. `delete` removes
  them. `--prune` (delete what isn't in the input) comes later.
- Every apply runs admission (both tiers) first. If anything fails, nothing
  is stored.
- **Incremental reconcile.** r7sd diffs the desired state against what it
  observes:
  - Unchanged plugins keep running without a gap.
  - Only changed jalv processes start or stop, and only changed connections
    are re-cabled.
  - A changed URI or bundle restarts that one plugin.
  - Parameter changes are sent live.

### Drift policy

- **Structure is reconciled continuously.** A crashed plugin gets restarted.
  An r7s-declared connection removed elsewhere (qpwgraph, etc.) is restored.
- **Parameters follow the spec, not the knob.** r7sd tracks the last value it
  sent each parameter. It sends a new one only when the desired value
  changes: an edit in the YAML, a parameter removed (back to the plugin
  default), or a rig switch. A live tweak from a native UI or MIDI CC
  therefore persists until the spec changes. A crashed plugin is relaunched
  with the current spec values.
  - `r7sctl diff -f` shows what an apply would change (exit 1 if anything).
  - `r7sctl snapshot RIG` prints the active rig with live values, to write
    back to YAML.

### Graph ownership

r7s manages only what it created: its plugin clients and the connections it
declared. It never removes connections it didn't create, even ones that touch
r7s ports or referenced devices. That makes it safe on a desktop where a DAW
or browser is also using the audio stack.

### Daemon and API

- **Process:** `r7sd` runs as a systemd user unit
  (`packaging/systemd/r7sd.service`). When PipeWire is running it re-execs
  itself under `pw-jack`, so it and every jalv child use PipeWire's libjack.
- **API:** JSON over HTTP on `$XDG_RUNTIME_DIR/r7s.sock` (mode 0600):

  | Method and path | Purpose |
  |---|---|
  | `GET /v1/status` | Daemon, JACK and backend health |
  | `GET /v1/events` | Recent events |
  | `GET /v1/resources[?kind=]` | List stored resources |
  | `GET /v1/resources/{Kind}/{name}` | One resource, with status |
  | `POST /v1/apply` | Admit and store (`{documents, dryRun}`) |
  | `POST /v1/diff` | What an apply would change |
  | `POST /v1/delete` | Delete (`{refs, dryRun}`) |
  | `GET /v1/snapshot/{rig}` | The active rig with live values |

  The client sends each document with its source file and a line map, so
  server-side diagnostics still point at `file:line`. File-relative
  `bundle:`, `pluginPaths` and `jalvPath` are made absolute client-side.
- **Admission:** tier 1 runs with strict references against the store merged
  with the incoming resources, and tier 2 runs on the incoming resources.
  Hardware problems are warnings, not errors: like a pending pod, the rig
  waits for the device.
- **State:** `~/.local/state/r7s/resources/<Kind>/<name>.json`. Each apply that
  changes a resource's spec bumps its `generation`. Status reports the store
  revision the reconciler last finished, which is what `apply --wait` waits
  for.
- **Reconciler:** one thread owns all side effects. API calls and JACK
  callbacks only wake it, and it also runs every 2s.
  - Rig phases: `Ready`, `Progressing`, `Degraded` (crash loop, unresolved
    edge, missing plugin), and `Inactive`.
  - Crashed plugins restart with exponential backoff (1s doubling to 30s).
- **Process safety:** jalv children start under `setpriv --pdeathsig KILL`,
  so they die with r7sd even on SIGKILL. The signal is tied to the spawning
  *thread*, so only the reconciler thread spawns and stops plugins.

## Validation (admission)

### Tier 1: offline/structural (runs anywhere)

- YAML syntax, envelope, and schema, including unknown fields (`extra:
  forbid`) and value ranges such as CC 0–127.
- Exactly one plugin format per entry, and the format must be supported.
- Plugin ids are unique within a rig and don't collide with device aliases.
- Endpoint syntax.
- Device port names are canonical, used in the right direction (capture
  only in `from`, playback only in `to`), and listed under the matching
  connection type (audio vs. midi).
- `from` ≠ `to`, and no duplicate edges.
- Graph cycle detection on plugin nodes. Device capture and playback are
  separate nodes, so a device → … → same device chain is not a cycle.
- References (device aliases, ActiveRig → Rig, `device:` on CC maps) resolve
  within the input set. In offline mode an unresolved reference is a warning
  (the daemon's store may define it). At apply time it is an error.

### Tier 2: environmental (`--live`; always run on apply)

- **Plugin checks**, from TTL metadata in the configured plugin paths:
  - The plugin URI is installed.
  - Endpoint ports exist, with the right direction and type (audio/midi).
  - Parameters name control input ports, and values are in range (or a valid
    scale-point label).
  - CC maps target control inputs.
- **Device checks**, from the live JACK graph and sysfs (phase 3): devices are
  present and the referenced ports exist.

```bash
r7sctl validate -f rig.yaml          # tier 1 only, runs anywhere
r7sctl validate --live -f rig.yaml   # tier 1 + tier 2
r7sctl apply -f rig.yaml             # validate both tiers, then reconcile
```

Diagnostics carry a severity (`error`/`warning`), the resource
(`Rig/guitar-rig`), a field path (`spec.connections.audio[2].to`), and a
message. `-o json` prints them as machine-readable output.

## LV2 metadata

r7s parses the bundles' TTL files itself with rdflib, in pure Python, so
`pip install` works on any architecture. The lilv Python bindings are an
optional fast path when installed.

- Bundles: `<path>/*.lv2/manifest.ttl` plus the files it links with
  `rdfs:seeAlso`.
- Per plugin: URI, name, class (category), binary, whether it has a native
  UI, and ports. Each port has index, symbol, name, direction, type
  (`audio | control | cv | midi | atom`), default/min/max, unit, properties
  (toggled, integer, enumeration, optional), and scale points.
- Parsed bundles are cached in `~/.cache/r7s/lv2/`, keyed by bundle path and
  TTL mtimes.

## CLI

```bash
# Plugin discovery (phase 1)
r7sctl plugins list [--category Reverb]
r7sctl plugins search reverb [--category ...]
r7sctl plugins inspect <uri>
r7sctl plugins scaffold <uri> [--id verb]   # YAML stanza, ranges as comments

# Validation (phase 1)
r7sctl validate [--live] -f <file|dir|-> [-R]

# Daemon operations (phase 2)
r7sctl apply -f ... [--dry-run] [--wait]
r7sctl get [rigs|devices|activerig|config|all] [NAME] [-o yaml|json]
r7sctl describe rig NAME | device NAME      # live status + events
r7sctl diff -f ...                          # exit 1 if apply would change things
r7sctl delete KIND NAME | -f ...            # refused if it would dangle a reference
r7sctl snapshot RIG                         # active rig with live parameter values
r7sctl status                               # daemon, JACK, backend health

# Hardware discovery (phases 2–3)
r7sctl audio ports [-o yaml]                # yaml: ready-to-apply Devices
r7sctl midi devices
r7sctl midi listen <device>
r7sctl midi sniff --timeout 30s

# Observability (phase 4)
r7sctl logs [--follow] [--since ...] [--unit plugin/amp] [--priority err]
r7sctl system-check
```

Every discovery and diagnostic command supports `-o json`.

## Execution modes

- **Headless** (`display: never`, or no display available): one jalv process
  per plugin without a UI. mod-host comes later as a single-process headless
  backend.
- **Desktop** (`display: always`, or X11/Wayland available): jalv with its
  GTK UI, so native LV2 UI windows open. Per-plugin `ui: false` opts out.
- **jalv spike result (2026-11-15).** r7sd drives the console `jalv` over
  stdin: `set SYMBOL VALUE` for live changes, `-c SYM=VAL` for launch values,
  and `quit` to stop. Findings, all verified by measuring the audio output
  with a JACK client:
  - Distro jalv 1.8.0 ignores `set` on port controls, because port controls
    never get a value type. Fixed upstream in 1.8.2 (commit `1e63ed5`), but
    1.8.2+ needs lilv ≥ 0.28, which Ubuntu 26.04 doesn't ship.
  - Its console spins at 100% CPU once stdin closes and then ignores SIGTERM.
    A command line over 1023 bytes overflows a stack buffer and aborts.
    Both bugs are still on upstream main.
  - `jalv.gtk3` doesn't read stdin at all, so it can't be the controlled
    process.
  - **Decision:** r7s uses a patched 1.8.0 built by
    `third_party/jalv/build.sh` (patches in `third_party/jalv/patches/`),
    installed to `~/.local/opt/r7s-jalv`. r7sd still never closes a jalv's
    stdin on purpose, and it falls back to SIGKILL on its own child if
    `quit` times out.
  - **Native UIs stay open.** The GTK frontend can't be controlled, so the
    desktop mode needs another way to get native windows (e.g. console jalv
    with `-s`, which this build supports through suil, or a separate UI-only
    process). This is still to be tested.

## Observability

A journalctl-style structured event stream. Event types:

- **xrun**: timestamp, active rig, DSP load.
- **plugin**: load/unload, crash/restart, parameter changes.
- **device**: hotplug/unplug, port registration, sample-rate changes.
- **midi**: device connect/disconnect, program changes. CC events are opt-in.
- **reconciler**: apply, validate, diff, errors.
- **system**: daemon start/stop, rig switch.

The backend is the systemd journal with structured fields. When r7sd isn't
running under systemd it falls back to an append-only JSON-lines file.

## Web UI (phase 5)

- Read-only, served by r7sd as a static page with no build step.
- One node per plugin and per device. Edges are labeled with ports
  (`capture_l → in`), and a toggle expands a port-level view.
- Live overlay: process status, xrun count, drift highlights.

## Distribution

- `pip install r7sctl` (Python ≥ 3.11).
- System deps: `pipewire-jack` (or `jackd`) and a jalv with working console
  `set` (≥ 1.8.2, or the patched build in `third_party/jalv/`). `mod-host` and
  `lilv-utils` are optional.
- Architecture-agnostic: x86_64, ARM64, ARM32, anywhere Linux + JACK runs.

## AI-assisted authoring

1. The AI runs `r7sctl plugins list -o json`, `r7sctl audio ports -o json`
   and `r7sctl midi devices -o json`.
2. The user describes the rig in natural language.
3. The AI resolves device IDs and plugin URIs from that live system state.
4. The AI runs `r7sctl plugins scaffold` to get parameter defaults and port
   names.
5. The AI generates the YAML. The user reviews it, then runs `validate`, then
   `apply`.

## Build phases

1. Schema, tier-1 validation, tier-2 plugin checks, TTL discovery, and the
   `plugins` and `validate` commands.
2. r7sd API and state store, jalv backend, device port resolution,
   incremental reconciler, and `apply`/`get`/`describe`/`diff`/`delete`/`snapshot`.
3. Tier-2 device checks, MIDI CC mapping, and `midi listen`/`sniff`.
4. Structured logs (journal/JSON), xrun events, `logs`, and `system-check`.
5. Web UI, then udev hotplug handling.

## Decision log (2026-11-15)

| Question | Decision |
|---|---|
| Control plane | Daemon API over a Unix socket, plus a state store |
| Parameter drift | Structure is reconciled continuously; parameters only on apply |
| First backend | jalv (mod-host and Carla later) |
| Multi-rig banks / Program Change | Deferred to v2. "Scene" is reserved for parameter snapshots |
| Primary kind name | `Rig` (selector: `ActiveRig`) |
| MIDI mapping | Inline on the parameter |
| Plugin formats | Format-tagged (`lv2:` / `vst3:` / `clap:`), LV2 only in v1, optional local `bundle:` |
| Plugin references | Full IDs only, no aliases |
| Graph ownership | Only what r7s created |
| File layout | Same as kubectl: file, stream, directory, stdin |
| Structural changes | Incremental reconcile |
| Web UI | Plugin nodes with port-labeled edges, a port-view toggle, read-only |
| LV2 metadata | Pure-Python TTL parsing (rdflib), with lilv as an optional fast path |
