# The Hermes -> python -> wsl.exe console popup

**Status:** finding + proposed patch. Nothing in `X:/Dev/hermes` was modified; Hermes is upstream
code and READ-ONLY for this task.

## What the watcher logged

`X:/Dev/swarm/popup_watch.ps1` (`X:/Dev/swarm/popups.log`, 2026-10-04 11:37:01):

```
NEW ConsoleWindowClass 'C:\Windows\System32\wsl.exe'
    146148(gone) python.exe []
    <= 186696 python.exe ["X:\Dev\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe" -m hermes_cli.main gateway run]
    <= 164768(gone, unseen)
```

Two facts survive the watcher's 400 ms polling and its pid cache:

* a **visible `ConsoleWindowClass` window titled `C:\Windows\System32\wsl.exe`** appeared, and
* its owner process descended from the Hermes gateway
  (`python -m hermes_cli.main gateway run`).

The intermediate `python.exe` row has an **empty** command line. That is the cache showing a pid
that had already exited and been reused, so the log cannot name the exact Hermes call site - and I
am not going to pretend otherwise. What the window title and class *are* enough to establish is the
mechanism below, and Hermes' own source says the same thing in its own words.

## The mechanism

**1. `bash` on this machine can be WSL.** Verified on this box:

| Path | FileDescription |
|---|---|
| `C:\Windows\System32\bash.exe` (90 KB) | `Microsoft Bash Launcher` |
| `C:\Windows\System32\wsl.exe` (168 KB) | `Microsoft Windows Subsystem for Linux Launcher` |

`System32\bash.exe` is the WSL stub: it hands off to `wsl.exe`. `CreateProcess` searches
`System32` **before** `PATH`, so any spawn of a bare `bash` (or `sh`) reaches WSL even when Git Bash
is installed and first on `PATH`. Hermes knows this - it is written down twice:

* `agent/skill_preprocessing.py:54-58` - *"CreateProcess searches System32 before PATH and may pick
  WSL's launcher. Reuse the terminal's native Git Bash resolution."* (and it does call
  `tools.environments.local._find_bash()`)
* `pm/shell.py` - *"PATH dirs whose bash.exe is not a shell: System32 holds the WSL launcher"*

**2. A console window only appears when the launching process had no console.** A `wsl.exe` that
inherits a console never shows a window; one started under a **console-less** parent allocates and
*shows* its own. This is not inference - it is Hermes' own conclusion, in two comments:

* `hermes_cli/_subprocess_compat.py:160-168` - *"a DETACHED_PROCESS child has NO console, so every
  console-subsystem descendant (git, gh, cmd, node, powershell, ...) allocates its own - a visible
  flash per spawn, including inside third-party libraries no per-site sweep can reach"* (#54220 /
  #56747).
* `gateway/run_shutdown.py:1465-1469` - *"Console python under CREATE_NO_WINDOW: nothing flashes.
  NOT pythonw.exe - a console-less watcher makes every console-subsystem descendant allocate a
  visible conhost (#54220/#56747)."*

So the popup is: **gateway (console-less at that instant) -> a python child that inherited no
console -> `wsl.exe` -> visible `ConsoleWindowClass`.** The fix for that shape is always the same
and always the same one line: give the child a *hidden* console (`CREATE_NO_WINDOW`) instead of no
console at all, so its descendants inherit something invisible.

**3. Why it can still happen: the invariant is per-call-site.** Hermes has the right helper
(`windows_hide_flags()` / `windows_detach_flags()` in `hermes_cli/_subprocess_compat.py`) and
excellent notes, but **nothing enforces it**. Measured over `hermes-agent` (excluding `tests/`,
`evals/`, `scripts/`, `apps/`, `node_modules/`, `venv*`, `optional-skills/`), by AST scan of every
`subprocess.run / Popen / call / check_output / check_call` call:

> **536 call sites pass no `creationflags` at all.**

Under a console-bearing parent those 536 are harmless - the child inherits the console and nothing
new appears. Under a console-less parent every one of them is a latent flash, and Hermes explicitly
supports and documents console-less parents: `pythonw` gateway and kanban/slash workers
(`hermes_bootstrap.py:302`, `_subprocess_compat.py:225`), VBS/service launch
(`gateway/run_startup.py:858-861`), and every `windows_detach_flags()` caller. Which is why the fix
keeps coming back as a new popup rather than as one known bad line: it is 536 chances, not one.

ABP hit exactly this bug and fixed it the only way that scales - one process-wide wrapper instead of
536 call sites. `bot/sandbox_ns/guard.py` replaces `subprocess.Popen` and rewrites
`creationflags`; `docs/sandbox-nervous-system.md` is the write-up. Hermes has no equivalent.

## Proposed patch (minimal, not applied)

One new function plus one call per entry point - about 40 lines, and it covers all 536 sites at
once instead of chasing them.

**a. `hermes_cli/_subprocess_compat.py`** - add next to the existing flag helpers:

```python
_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008

_original_popen = None
_installed = False
_visible_depth = 0


def rewrite_flags(flags: int) -> int:
    """The creationflags a spawn should really use (pure, so the rules are testable)."""
    if not IS_WINDOWS:
        return flags
    if _visible_depth:                                 # a person asked for a console: leave them one
        if flags & _DETACHED_PROCESS and not flags & _CREATE_NEW_CONSOLE:
            return (flags & ~_DETACHED_PROCESS) | _CREATE_NEW_CONSOLE
        return flags
    if flags & _CREATE_NEW_CONSOLE:
        return (flags & ~_CREATE_NEW_CONSOLE) | _CREATE_NO_WINDOW
    if flags & _DETACHED_PROCESS:                     # CREATE_NO_WINDOW is *ignored* when combined
        return (flags & ~_DETACHED_PROCESS) | _CREATE_NO_WINDOW   # with DETACHED_PROCESS (MSDN)
    return flags | _CREATE_NO_WINDOW


def install() -> bool:
    """Idempotent, no-op off Windows. Returns whether the guard is in place."""
    global _original_popen, _installed
    if not IS_WINDOWS or _installed:
        return _installed
    _original_popen = subprocess.Popen
    subprocess.Popen = _guarded_popen
    _installed = True
    return True


@contextlib.contextmanager
def visible():
    """The one honest exception: a console a person is meant to see (see the list below)."""
    global _visible_depth
    _visible_depth += 1
    try:
        yield
    finally:
        _visible_depth -= 1
```

where `_guarded_popen` is the four-line wrapper `guard.py` uses: read `kwargs.get("creationflags")`,
`rewrite_flags` it, put it back, call the saved original. Idempotent by construction (the saved
original is captured before the swap), which matters because `main.py` runs before every entry point.

**b. Call it next to the existing early Windows hardening** in `hermes_cli/main.py:36`, right
after `suppress_platform_ver_console()` - that is already the "fix Windows before anything heavy
imports" seam, and it is on the `hermes gateway run` path:

```python
from hermes_cli._subprocess_compat import install as install_no_window, suppress_platform_ver_console

suppress_platform_ver_console()
install_no_window()
```

and in the other process entry points that do not go through `hermes_cli.main`: the gateway
watcher/restart workers (`gateway/run_shutdown.py:_spawn_windows_restart_watcher`),
`hermes_cli/main_dashboard.py`, `tui_gateway/entry.py`, and the cron worker scripts
(`cron/scheduler_script.py`) - same one-liner, same reason as ABP's `bot/main.py`, `abp_cli`,
`abp_run`, `abp_acp`, `bot/tui`, `bot/mcp_server.py`.

**c. Wrap the deliberate interactive spawns in `visible()`.** The audit above finds a handful that
*want* a console and currently get one by accident:

* `hermes_cli/web_routers/profiles.py:935` - `["cmd.exe", "/c", "start", "", command]` (opens a
  terminal on purpose; it is also the one place a new console is what the caller asked for)
* `hermes_cli/cli_commands_mixin.py:2405`, `config.py:3188`, `journey.py:331` - `$EDITOR` /
  `notepad` on a config file
* `hermes_cli/main_dashboard.py:423` - the re-exec that hands the terminal over
* `cron/scheduler_script.py` interactive runs

Without step (c) the guard turns these into silent no-window launches, which is the other failure
mode. With it they keep working and everything else becomes windowless.

**d. Belt and braces, cheap:** resolve `bash` through `tools.environments.local._find_bash()` (or
strip `System32` from `PATH` when spawning a shell) at every site that spawns a bare `bash`/`sh`.
Hermes already does this in the two places it noticed; the guard makes it unnecessary for *windows*
but not for *"why did my `bash -c` run inside WSL instead of Git Bash"*.

## How to check the patch worked

1. Leave `X:/Dev/swarm/popup_watch.ps1` running for a normal Hermes day and confirm no new
   `ConsoleWindowClass` row names a Hermes descendant.
2. Hermes' own suite: add the ABP-shaped test - a `pytest` plugin that polls `EnumWindows` for
   `ConsoleWindowClass` / `CASCADIA_HOSTING_WINDOW_CLASS` / `PseudoConsoleWindow` and fails the
   test that was running when a new one appears with its process chain. Working implementation:
   `tests/no_windows_plugin.py` in the ABP checkout (same three window classes).
3. Hermes already has `scripts/check-windows-footguns.py`; a "no `subprocess` call without
   `creationflags`" check belongs there as the second line of defence, once the guard exists (today
   it would report 536 hits and drown the signal).

## Note for ABP

ABP's half of this chain is already covered. The Hermes spawn ledger
(`X:/Dev/hermes/spawn-ledger.json`) shows the gateway running ABP's `bot.mcp_server` as an
`mcp-helper`, and `bot/mcp_server.py` installs `bot.sandbox_ns.guard` at start-up, so ABP processes
under Hermes are windowless. The popup above is Hermes' own subprocess layer.
