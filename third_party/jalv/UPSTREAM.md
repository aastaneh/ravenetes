# jalv bugs to report upstream

Found on 2026-11-15 while testing jalv as the r7s backend. Bug 1 and bug 2
are still present on upstream `main` at the v1.10.0 tag (`a0f0162`); recheck
before filing. Bug 3 is already fixed upstream and is listed only to explain
why patch 0001 exists.

Upstream: https://gitlab.com/drobilla/jalv (GitHub mirror: drobilla/jalv)

The fixes for bugs 1 and 2 are in
`patches/0002-Quit-console-on-stdin-EOF-and-bound-command-length.patch`.
That patch is against v1.8.0, but the code it changes is unchanged on main.

---

## Bug 1: console frontend busy-loops at 100% CPU after stdin EOF

**Component:** `src/console/jalv_console.c`, `jalv_frontend_run()`, `USE_POLL`
branch

**What happens:** When stdin reaches EOF (pipe closed, `< /dev/null`, parent
exits), `poll()` reports the fd readable on every iteration and `read()`
returns 0. Only `n == 1` is handled, so the loop spins forever at about 100%
of one core and never quits. Observed with jalv 1.8.0: the process kept
running after SIGTERM (state `R`, ~98% CPU) until killed with SIGKILL.

**Expected:** EOF on stdin quits, like the `fgets()` branch already does
(`fgets()` returning NULL → `COMMAND_QUIT`).

**Repro:**

```sh
jalv urn:zamaudio:ZamVerb < /dev/null &   # any plugin
sleep 3; ps -o pid,stat,%cpu,cmd -p $!    # STAT R, ~100% CPU, never exits
```

**Fix:** treat `read()` returning 0 as quit:

```c
const ssize_t n = read(STDIN_FILENO, line + len, 1);
if (n == 0) {
  st = COMMAND_QUIT; // EOF
} else if (n == 1) {
  ...
```

(`-i` avoids reading stdin, but a process that is driven over stdin and then
loses its controller should exit, not spin.)

---

## Bug 2: stack buffer overflow on a console line of 1024+ bytes

**Component:** same loop. `char line[1024]` is filled one byte at a time with
`++len` and no bounds check.

**What happens:** A line of 1024 bytes or more without a newline writes past
the end of `line` on the stack. With glibc's stack protector this aborts
(SIGABRT, "stack smashing detected"). Without it, adjacent stack memory is
corrupted. Anything that can write to jalv's stdin can trigger it.

**Repro:**

```sh
{ sleep 2; printf '%5000s\n' | tr ' ' x; sleep 1; } | jalv urn:zamaudio:ZamVerb
echo $?   # 134 (SIGABRT) on Ubuntu 26.04's jalv 1.8.0
```

**Fix:** stop before the buffer fills, report an error, and discard the rest
of that line so its tail isn't run as a command:

```c
} else if (discarding) {
  // skip until newline
} else if (len + 2U >= sizeof(line)) {
  fprintf(stderr, "error: command too long\n");
  discarding = true;
  len        = 0;
} else {
  ++len;
}
```

On `'\n'`: if `discarding`, clear it instead of running the command.

---

## Bug 3 (already fixed upstream): console `set` does nothing for port controls

Fixed in jalv 1.8.2 by commit `1e63ed5` "Fix port controls":
`new_port_control()` didn't set `value_type`, so `set_control_from_string()`
matched none of its type branches and silently returned. Patch 0001 backports
it to 1.8.0. Worth raising with the Debian/Ubuntu maintainers: 26.04 ships
1.8.0, and 1.8.2 can't be packaged there as-is because it needs lilv ≥ 0.28.

---

## Observation (not filed yet): console frontend with `-s`

Seen with 1.8.0 + our patches, ZamVerb (DPF UI with `ui:showInterface`):

- **Command loop skipped:** while the custom UI is shown, the console never
  reads stdin. `jalv_frontend_run()` only enters the command loop when
  `run_custom_ui()` returns false, so `set`, `controls` and `quit` are
  ignored for the window's lifetime. This may be intended, but it makes
  `-s` unusable for a host that drives jalv over stdin. A fix would poll
  stdin inside the custom-UI loop, reusing the command parser.
- **Close ignored:** asking the window manager to close the window (i3
  `kill`, i.e. `WM_DELETE_WINDOW`) left the window mapped and jalv running.
- **Segfault on stop:** SIGTERM then terminated jalv with SIGSEGV (rc -11)
  instead of a clean shutdown.

These are recorded but not root-caused. Reproduce with
`jalv -s urn:zamaudio:ZamVerb`, type `controls` (no output), then close the
window and send SIGTERM.

---

## Bug 4: a preset URI on the command line restores an empty state

**Component:** `src/jalv.c`, `open_plugin_state()`, URI branch

**What happens:** given a preset URI as `PLUGIN_STATE`, jalv calls
`lilv_state_new_from_world()` without first calling
`lilv_world_load_resource()` on that URI. Presets saved by hosts (jalv's own
included) keep their data in the file the manifest points to with
`rdfs:seeAlso`; only the manifest is loaded at startup. So the state is
built from the manifest alone: the right plugin, but no port values and no
`state:state`. The plugin starts as if no preset were given. The console's
`preset URI` command does call `lilv_world_load_resource()` first, which is
why it works.

**Repro** (any plugin whose state matters, e.g. drumlabooh with a kit):

```sh
jalv.gtk3 <plugin-uri>              # pick a kit, save preset "kit"
jalv file:///home/you/.lv2/<plugin>_kit.preset.lv2/kit.ttl   # silent: no kit
jalv /home/you/.lv2/<plugin>_kit.preset.lv2/kit.ttl          # works (path branch)
```

**Fix:** call `lilv_world_load_resource(jalv->world, state_uri)` before
`lilv_state_new_from_world()` in the URI branch.

**r7s workaround:** file-backed presets are passed as paths.
