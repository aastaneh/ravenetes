# Ravenetes (r7s)

> [!WARNING]
> **Use at your own risk.**
>
> - **Active development:** things will change and break.
> - **Vibe coded:** this software was written entirely with AI assistance. I
>   built it for my own enjoyment.
> - **Live use:** don't rely on it for a live performance or gig until you've
>   tested your exact rig, on your exact hardware, yourself.
>
> It comes **as-is, with no warranty of any kind**. You use it at your own
> risk.

**Declare your audio rig in YAML. r7s makes it real and keeps it that way.**

r7s is a Kubernetes-style host for LV2 plugins on Linux. You describe the
rig declaratively:
- plugins
- their parameters
- MIDI knob mappings
- every cable between hardware and plugins

A daemon, `r7sd`, starts the plugins, wires them up in JACK/PipeWire, and
keeps them that way:
- crashed plugins restart
- pulled cables come back
- unplugged devices reconnect when you plug them back in

It replaces graphical patchbays like MOD/modep with something you can
version in git, diff, and apply over SSH. It runs on anything with Linux and
JACK, from a laptop to a Raspberry Pi with an audio HAT.

```
 rig.yaml ──► r7sctl apply ──► r7sd ──► jalv (one per plugin) ──► PipeWire/JACK ──► your hardware
                                 │
                                 └──► web UI (live graph), event log, MIDI CC mapping
```

What a rig looks like (excerpt from [`examples/beatstep-303/rig.yaml`](examples/beatstep-303/rig.yaml)):

```yaml
apiVersion: r7s/v1
kind: Device
metadata: {name: pro}
spec: {usb: "1c75:0287"}            # Arturia BeatStep Pro
---
apiVersion: r7s/v1
kind: Rig
metadata: {name: beatstep-303}
spec:
  plugins:
    - id: synth
      lv2: "http://distrho.sf.net/plugins/Nekobi"
      parameters:
        cutoff: {value: 40, cc: 10, channel: 11, device: pro}   # knob 1 moves the filter
    - id: verb
      lv2: "urn:zamaudio:ZamVerb"
      parameters: {master: -12, wetdry: 30}
  connections:
    midi:
      - {from: "pro:midi_capture", to: "synth:lv2_events_in"}
    audio:
      - {from: "synth:lv2_audio_out_1", to: "verb:lv2_audio_in_1"}
      - {from: "verb:lv2_audio_out_1",  to: "speakers:playback_l"}
```

---

## Contents

- [Install](#install)
- [First rig in five minutes](#first-rig-in-five-minutes)
- [Discovering your hardware](#discovering-your-hardware)
- [Installing LV2 plugins](#installing-lv2-plugins)
- [Writing rigs](#writing-rigs)
- [Day-to-day commands](#day-to-day-commands)
- [Web UI](#web-ui)
- [Logs and troubleshooting](#logs-and-troubleshooting)
- [Running on a Raspberry Pi](#running-on-a-raspberry-pi)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Development](#development)

---

## Install

r7s is tested on Ubuntu 26.04 with PipeWire. Other distributions should work
if they have the same pieces.

### 1. System packages

```bash
sudo apt install pipewire-jack                 # JACK on top of PipeWire (most desktops have PipeWire)
sudo apt install meson ninja-build pkg-config gcc \
  libjack-jackd2-dev liblilv-dev libsuil-dev libserd-dev \
  libsord-dev libsratom-dev libzix-dev lv2-dev # to build jalv (next step)
```

### 2. A jalv that can change parameters live

r7s runs each plugin in [jalv](https://drobilla.net/software/jalv), driven
over stdin. **Ubuntu's jalv 1.8.0 silently ignores live parameter changes**,
and 1.8.2+ needs a newer lilv than Ubuntu ships. So build the patched 1.8.0
included here. It installs to `~/.local/opt/r7s-jalv`, alongside the system
jalv without replacing it:

```bash
third_party/jalv/build.sh
```

The patches and the bugs behind them are documented in
[`third_party/jalv/`](third_party/jalv/UPSTREAM.md).

### 3. r7s itself

r7s isn't on PyPI yet, so install from this checkout with
[uv](https://docs.astral.sh/uv/):

```bash
uv tool install --editable .        # puts r7sctl and r7sd in ~/.local/bin
r7sctl system-check                 # checks everything above; fixes are printed inline
```

`--editable` means the installed tools run this checkout, so `git pull`
updates them.

### 4. Run the daemon

As a systemd user service, so it starts with your session:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/r7sd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now r7sd
r7sctl status
```

Or run `r7sd` in a terminal while you experiment. Either way, r7sd connects
to PipeWire through its JACK layer by itself (it re-runs itself under
`pw-jack`).

---

## First rig in five minutes

```bash
# 1. Find your hardware: these print ready-to-use Device resources
r7sctl audio ports -o yaml > devices.yaml
r7sctl midi devices -o yaml >> devices.yaml

# 2. Find plugins and get a ready-made YAML entry for one
r7sctl plugins search reverb
r7sctl plugins scaffold urn:zamaudio:ZamVerb --id verb

# 3. Write rig.yaml (a Rig + an ActiveRig; see "Writing rigs"), then:
r7sctl validate --live -f devices.yaml -f rig.yaml   # checks plugins, ports, values, hardware
r7sctl apply -f devices.yaml -f rig.yaml --wait      # starts it; waits until it's Ready
r7sctl describe rig my-rig                           # plugins, connections, events
r7sctl ui --open                                     # the live graph in your browser
```

Edit the YAML and `apply` again whenever you like. r7s changes only what you
changed: untouched plugins keep running without a gap.

---

## Discovering your hardware

r7s refers to hardware by **stable IDs** (USB/PCI vendor:product, or ALSA
card name), never by JACK port names, which change between boots and
machines.

### Audio interfaces

```bash
r7sctl audio ports
```
```
BUS  ID         DEVICE                                  DIRECTION  SELECTOR  CH  PORTS
pci  8086:51c8  Alder Lake PCH-P High Definition Audio  playback   Speaker   2   playback_FL, playback_FR
pci  8086:51c8  Alder Lake PCH-P High Definition Audio  capture    Mic2      2   capture_FL, capture_FR
```

`r7sctl audio ports -o yaml` prints Device resources you can paste straight
into a rig. A card with several outputs (speakers, HDMI…) gets a **selector**
(`playback: Speaker`) that picks one. Most USB interfaces have one input and
one output, so they need no selector.

### MIDI controllers

```bash
r7sctl midi devices                 # every MIDI device, with its ID and ports
r7sctl midi devices -o yaml         # as Device resources
```

### Which knob sends which CC?

```bash
r7sctl midi listen usb/1c75:0287 --as pro
```
Turn a knob and you get a line ready to paste onto a parameter:
```
{ cc: 10, channel: 11, device: pro }  # value 64
```

Or turn everything you care about in one go:

```bash
r7sctl midi sniff --timeout 30s     # summary of every control you touched, with ranges
```

Many controllers only send CCs in a particular mode. On an Arturia BeatStep
Pro, for example, press **CNTRL** first.

### How devices are identified

| In the Device spec | Use it for | Example |
|---|---|---|
| `usb: "vvvv:pppp"` | USB interfaces and controllers (`lsusb`) | `usb: "1235:8210"` |
| `serial: "…"` | two identical USB units | `serial: "Y8ABCDEF"` |
| `pci: "vvvv:pppp"` | built-in and PCI(e) sound cards (`lspci -nn`) | `pci: "8086:51c8"` |
| `alsa: name` | cards with no USB/PCI ID: Pi HATs, virtual cards | `alsa: pisound` |
| `capture:` / `playback:` | pick one endpoint of a multi-endpoint card | `playback: Speaker` |

Use exactly one of `usb`, `pci` or `alsa` per Device.

---

## Installing LV2 plugins

r7s isn't a package manager; install plugins however you like. It finds them
in these directories, in order:

| Path | What goes there |
|---|---|
| `~/.lv2/` | **your own downloads** (no sudo); the recommended place |
| `/usr/local/lib/lv2/` | system-wide manual installs (needs sudo) |
| `/usr/lib/lv2/`, `/usr/lib/<arch>/lv2/` | distro packages (`apt install …`); leave these to apt |

### From your distribution

```bash
sudo apt install lsp-plugins-lv2 zam-plugins x42-plugins dpf-plugins-lv2 calf-plugins
```

### Downloaded / precompiled plugins

An LV2 plugin is a **folder ending in `.lv2`** containing `manifest.ttl`,
other `.ttl` files and `.so` libraries. Copy the whole folder:

```bash
mkdir -p ~/.lv2
cp -r ~/Downloads/SomePlugin.lv2 ~/.lv2/
r7sctl plugins search someplugin           # shows up immediately
```

- **Folder depth:** it has to be `~/.lv2/SomePlugin.lv2/manifest.ttl`, not
  nested one level deeper.
- **Architecture:** match your CPU. `file ~/.lv2/SomePlugin.lv2/*.so` should
  say x86-64 on a PC, and aarch64 (or ARM for a 32-bit OS) on a Pi.
- **Libraries:** `ldd ~/.lv2/SomePlugin.lv2/*.so | grep "not found"` lists
  missing dependencies.
- **Pickup:** `r7sctl` picks new plugins up at once. The running daemon picks
  them up at your next `apply`, with no restart needed.

To use other directories, set them in a Config resource (`pluginPaths`), in
`~/.config/r7s/config.yaml`, in `$LV2_PATH`, or per command with
`--plugin-path`.

### Exploring a plugin

```bash
r7sctl plugins list --category Reverb
r7sctl plugins inspect urn:zamaudio:ZamVerb     # ports, parameters (ranges, units, labels), presets
r7sctl plugins scaffold urn:zamaudio:ZamVerb --id verb   # paste-ready YAML with ranges as comments
```

### Plugins with state (drum kits, samples, IRs): presets

Some plugins keep important settings outside their parameters: a drum
sampler's kit, a convolution reverb's impulse file, a JUCE plugin's internal
state. Set them once in the plugin's own window and save a preset:

```bash
pw-jack jalv.gtk3 https://github.com/psemiletov/drumlabooh   # pick a kit, then save a preset from the menu
r7sctl plugins inspect https://github.com/psemiletov/drumlabooh   # lists your preset
```

Then reference the preset in the rig, by label or URI:

```yaml
- id: drums
  lv2: "https://github.com/psemiletov/drumlabooh"
  preset: my-drum-kit
```

`preset:` and `parameters:` work together:
- **Preset values come first.** The preset's control values become the
  plugin's starting point.
- **Your values win.** Anything in `parameters` overrides the preset.
- **Changing the preset** restarts only that plugin.

---

## Writing rigs

A rig file is one or more YAML documents of four kinds.

### `Device`: a piece of hardware

See [How devices are identified](#how-devices-are-identified).

### `Rig`: plugins, parameters and cables

```yaml
apiVersion: r7s/v1
kind: Rig
metadata: {name: guitar-rig}
spec:
  plugins:
    - id: drive                                 # your name for it (used in connections)
      lv2: "http://drobilla.net/plugins/mda/Overdrive"
      parameters:
        drive: 0.6                              # plain value
        muffle: {value: 0.2, cc: 16, device: midimix}             # value + knob
        output: {value: 0.5, cc: 17, channel: 1, device: midimix, min: 0.2, max: 0.8}
    - id: verb
      lv2: "urn:zamaudio:ZamVerb"
      preset: Default                           # optional: restore a preset first
      bundle: ./plugins/ZamVerb.lv2             # optional: ship a plugin with the rig
  connections:
    audio:
      - {from: "scarlett:capture_1",    to: "drive:left_in"}
      - {from: "drive:left_out",        to: "verb:lv2_audio_in_1"}
      - {from: "verb:lv2_audio_out_1",  to: "scarlett:playback_l"}
    midi:
      - {from: "midimix:midi_capture",  to: "synth:event_in"}
```

**Parameters:**
- **Which name:** use the plugin's port symbol (shown by `inspect` and
  `scaffold`), in the plugin's own units.
- **Value types:** a number, `true`/`false` for toggles, or a label for
  named values (`mode: Lead`).
- **Validation:** values are checked against the plugin's ranges.

**Knob mappings:**
- **The mapping:** `cc` is 0–127; `channel` is 1–16 (leave it out to accept
  any channel); `device` is a Device name.
- **Ranges:** `min`/`max` default to the parameter's full range.
  Logarithmic, integer, toggle and named-value parameters scale sensibly.
- **Knobs stay put:** a knob's position persists, and r7s won't snap it back.
  Only editing that value in the YAML overrides it.

**Port names:**

| Endpoint | Means |
|---|---|
| `drive:left_in` | plugin `drive`, port `left_in` (port names from `inspect`) |
| `scarlett:capture_1` … `capture_N` | device input channels (`capture_l`/`capture_r` = 1/2) |
| `scarlett:playback_1` … | device output channels (`playback_l`/`playback_r` = 1/2) |
| `midimix:midi_capture` | MIDI coming *from* a device (`midi_capture_2` for a second port) |
| `midimix:midi_playback` | MIDI going *to* a device |
| `usb/1235:8210/capture_1` | inline hardware, no Device needed |

Connecting two outputs to one input mixes them, and one output can feed many
inputs.

**Routing MIDI by channel:** a MIDI connection carries every channel. To
give each instrument its own channel (e.g. a sequencer's tracks), put a
channel filter between them, such as `x42-plugins`' **MIDI Simple Channel
Filter** (`http://gareus.org/oss/lv2/midifilter#onechannelfilter`). The
[beatstep example](examples/beatstep-303/rig.yaml) does exactly this.

### `ActiveRig`: which rig is playing

```yaml
apiVersion: r7s/v1
kind: ActiveRig
metadata: {name: current}
spec: {rig: guitar-rig}
```

Keep several Rigs stored and switch by applying a different ActiveRig. r7s
keeps plugins that both rigs share running across the switch.

### `Config` (optional)

```yaml
apiVersion: r7s/v1
kind: Config
spec:
  pluginPaths: [~/.lv2, /usr/lib/lv2]     # override plugin search paths
  log:
    backend: auto                         # auto | journal | file
    midiCC: false                         # true: log every knob value (verbose)
  ui:
    enabled: true
    listen: 127.0.0.1:7377                # 0.0.0.0:7377 to expose the read-only UI on your LAN
```

More examples: [`examples/`](examples/). The full design, including
reconcile semantics and why each choice was made, is in
[`docs/DESIGN.md`](docs/DESIGN.md).

---

## Day-to-day commands

| Command | What it does |
|---|---|
| `r7sctl apply -f FILE… [--wait] [--dry-run]` | validate and apply; only changes are applied |
| `r7sctl diff -f FILE…` | what `apply` would change (exit 1 if anything) |
| `r7sctl get [rigs\|devices\|all] [-o yaml]` | what's stored, with live status |
| `r7sctl describe rig NAME` | plugin states, connections (with reasons), recent events |
| `r7sctl snapshot RIG > saved.yaml` | the rig with **current knob positions**: save a sound |
| `r7sctl delete rig NAME` / `delete -f FILE` | remove (refused if something still refers to it) |
| `r7sctl status` | daemon, JACK, backend and web UI at a glance |
| `r7sctl ui [--open]` | the web UI's address |
| `r7sctl logs [-f] [-p err] [-u plugin/NAME]` | the event log |
| `r7sctl system-check` | is everything installed and working? |
| `r7sctl validate [--live] -f FILE…` | check without applying; `--live` also checks plugins and hardware |

Every command takes `-o json`. Files can be directories (`-R` to recurse) or
`-` for stdin.

---

## Web UI

`r7sctl ui --open` opens a **read-only** live view:
- **The signal graph:** hardware inputs on the left, plugins in signal
  order, outputs on the right, with problems highlighted (crashed plugins,
  missing devices, cables that can't connect, with the reason).
- **Details panel:** click a plugin to see every parameter, its **live
  value as you turn knobs**, the value in your YAML, and which knob controls
  it.
- **Views:** "Plugins" shows one box per plugin; "Ports" shows every cable.
- **Live events** as they happen.

It listens on `localhost:7377` only, unless you set `Config.spec.ui.listen`.
To view a headless Pi from your laptop:

```bash
ssh -L 7377:localhost:7377 pi      # then open http://localhost:7377
```

---

## Logs and troubleshooting

```bash
r7sctl system-check                # start here
r7sctl logs -p warning --since today
r7sctl logs -f -n 0                # watch live: crashes, xruns, devices, applies
r7sctl logs -u plugin/drums        # one plugin's history
r7sctl logs -t xrun                # audio dropouts, with DSP load
```

Events go to the systemd journal (`journalctl --user -t r7sd` works too), or
to `~/.local/state/r7s/events.jsonl` where there's no journal.

| Symptom | Likely cause |
|---|---|
| Knob mappings do nothing | The controller isn't sending CCs (wrong mode or channel). Check with `r7sctl midi listen`. |
| Turning a knob also changes a synth's volume or filter | MIDI connections pass *all* messages, including mapped CCs. The synth may respond to that CC itself (CC 7 is volume). Use a different CC, or a MIDI filter. |
| A sampler or drum plugin is silent | It has no kit or samples loaded. Save a preset with them and use `preset:`. |
| `system-check`: jalv "ignores live parameter changes" | You're on distro jalv 1.8.0. Run `third_party/jalv/build.sh`. |
| A device shows `PRESENT no` | Unplugged, or the wrong ID/selector. `r7sctl describe device NAME` says why; it reconnects by itself when it appears. |
| An xrun "during a graph change" | Normal: PipeWire hiccups once when plugins start or stop. Xruns *without* that note mean real overload. |

---

## Running on a Raspberry Pi

r7s is architecture-independent (Python, plus a jalv you build on the Pi).

- **Install:** the same steps as above. `third_party/jalv/build.sh` builds
  natively on the Pi.
- **Audio HATs** (Pisound etc.) have no USB/PCI ID: use `alsa: <card name>`
  (see `r7sctl audio ports` / `cat /proc/asound/cards`).
- **Plugins:** install ARM builds (`aarch64`, or `armhf` on a 32-bit OS).
- **Driving it from your laptop:**
  ```bash
  ssh pi r7sctl apply -f - < rig.yaml
  ssh pi r7sctl logs -f -n 0
  ```
- **Performance:** `r7sctl system-check` flags missing realtime priority
  and the CPU frequency governor (`performance` avoids xruns).

---

## Limitations

- **No plugin windows.** Plugins run headless. jalv can't show a plugin's
  own GUI and still take live parameter changes, so `ui: true` only produces
  a warning. Use presets for settings that need the GUI. Native windows are
  planned via Carla.
- **LV2 only.** VST3/CLAP are reserved in the schema; see the roadmap.
- **PipeWire for hardware.** Under plain jackd, plugins and plugin-to-plugin
  cables work, but hardware connections can't be resolved.
- **The web UI is read-only by design.** Changes go through YAML.
- **Mapped knobs and MIDI connections overlap.** A CC mapping doesn't remove
  that CC from MIDI connections; see troubleshooting.

---

## Roadmap

Planned or under consideration, roughly in priority order:

- **Rig banks and Program Change:** switch rigs (or parameter "Scenes"
  within a rig) from a foot controller's program changes.
- **Carla backend:** native plugin windows, plus VST3, CLAP and VST2
  plugins.
- **mod-host backend:** a single-process headless engine for small
  devices.
- **MIDI filtering on connections** (e.g. `channels: [10]`), without a
  separate filter plugin.
- **`r7sctl restart plugin/NAME`**, and a clean restart that isn't logged as
  a crash.
- **Hardware resolution under plain jackd.**
- **`apply --prune`**, to remove stored resources that aren't in the files
  being applied.
- **PyPI packaging** (`pip install r7sctl`) and Pi images.
- **Upstream jalv fixes**, so the patched build becomes unnecessary (see
  [`third_party/jalv/UPSTREAM.md`](third_party/jalv/UPSTREAM.md)).

---

## Development

```bash
uv sync
uv run pytest                        # Python tests (no audio stack or plugins needed)
node --test tests/js/*.test.mjs      # web UI layout tests
uvx ruff check src tests
```

The tests use fakes for jalv, JACK, PipeWire, sysfs and the ALSA sequencer,
plus small LV2 fixture bundles, so they run anywhere.

| Path | What's there |
|---|---|
| `src/r7s/` | CLI (`cli*.py`), daemon (`daemon.py`, `api.py`), reconciler, hardware (`hw/`), LV2 metadata (`lv2/`), web UI (`ui/`) |
| `docs/DESIGN.md` | design, decisions and their reasons |
| `examples/` | example rigs |
| `third_party/jalv/` | patched jalv build, and upstream bug write-ups |
| `packaging/systemd/` | the `r7sd` user service |

---

## License

[MIT](LICENSE). The patches in `third_party/jalv/` modify jalv, which is
ISC-licensed.
