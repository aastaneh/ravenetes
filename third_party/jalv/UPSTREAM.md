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
