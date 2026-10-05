"""No console window may ever appear on this person's desktop (the #1 complaint about ABP).

ABP starts a lot of processes, from a lot of places, and most of them were started with no
console flag at all. On Windows that means the child inherits (or is handed) a console, and
anything it starts in turn - `npm`, `git`, `cargo`, `npx`, a Python script that shells out -
puts a window on the desktop. Fixing that call site by call site is how it keeps coming back.

So this module fixes it once: `install()` makes `subprocess.Popen` a subclass that rewrites the
creation flags, and from then on every process ABP starts is windowless unless somebody
deliberately asked otherwise. Because `subprocess.run`, `subprocess.call`,
`subprocess.check_output` and asyncio's Windows subprocess transport all go through
`subprocess.Popen`, one wrapper covers all of them (asyncio included - see
`tests/test_sandbox_ns.py`, which checks that for real). It is a subclass rather than a
function because third-party code subscripts `subprocess.Popen` in annotations that are evaluated
at import time - see `_make_wrapper()`.

**The two flags that are not interchangeable**, which is why a single answer is not enough:

* `DETACHED_PROCESS` gives the child **no console at all**. That is the trap: a process with
  no console makes every console program it starts afterwards allocate and *show* a new
  window - the opposite of what "detached" is usually wanted for. So a bare
  `DETACHED_PROCESS` is rewritten to `CREATE_NO_WINDOW`.
* `CREATE_NO_WINDOW` runs the child against a **hidden console it owns**. Its own children
  inherit that hidden console and stay invisible, which is what a background job wants. This
  is the default for everything.

The honest exceptions are commands a *person* is meant to look at or type into - a module's TUI
window (`open_tui`), and the TUI handing the terminal to a container shell. Those go inside
`visible()`, and only there is `CREATE_NEW_CONSOLE` kept (or a `DETACHED_PROCESS` turned into it,
since Windows refuses both flags at once).

Idempotent, thread-safe enough for a boot-time call, and a no-op off Windows: `visible()`
still works there (it just changes nothing), because call sites should not have to care.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from typing import Iterator

# Spelled out rather than read off subprocess: these constants only exist there on Windows,
# and this module has to import cleanly everywhere (it is called from every entry point).
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

_lock = threading.RLock()
_original_popen = None
_wrapper_popen = None
_installed = False
_visible_depth = 0
_converted = 0


def _event(kind: str, detail: str) -> None:
    """Tell the registry, best effort: the nervous system's event log must never be the reason a
    spawn fails, so a failure to record one is swallowed."""
    try:
        from bot.sandbox_ns.registry import registry

        registry.event(kind, detail=detail)
    except Exception:  # noqa: BLE001
        pass


def rewrite_flags(flags: int) -> int:
    """The creationflags a spawn should really use (pure, so the rules are testable)."""
    if os.name != "nt":
        return flags
    if _visible_depth:                                    # a person asked for a console: leave them one
        if flags & DETACHED_PROCESS and not flags & CREATE_NEW_CONSOLE:
            return (flags & ~DETACHED_PROCESS) | CREATE_NEW_CONSOLE
        return flags
    if flags & CREATE_NEW_CONSOLE:
        return (flags & ~CREATE_NEW_CONSOLE) | CREATE_NO_WINDOW
    if flags & DETACHED_PROCESS:
        # The whole reason this wrapper exists: a console-less process makes every console
        # program it later starts pop a visible window, while a hidden console is inherited.
        return (flags & ~DETACHED_PROCESS) | CREATE_NO_WINDOW
    return flags | CREATE_NO_WINDOW


def _make_wrapper(original):
    """`original` with the rewrite above baked into its constructor.

    A **subclass**, not a wrapper function, and that is not a style choice: `subprocess.Popen` is
    subscripted by real third-party code - `popen_obj: subprocess.Popen[bytes]` in
    `mcp/os/win32/utilities.py`, evaluated while the class body runs, because that module does not
    use `from __future__ import annotations`. Substituting a plain function made the whole `mcp`
    package unimportable (`TypeError: 'function' object is not subscriptable`) the moment this
    guard was installed, which is how the test session found it. A subclass stays a class: it is
    subscriptable, `isinstance(proc, subprocess.Popen)` still holds, and every other attribute
    (including `__del__`'s "still running" warning) is the real one.
    """

    class WindowlessPopen(original):                      # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            global _converted
            flags = kwargs.get("creationflags") or 0
            rewritten = rewrite_flags(flags)
            if rewritten != flags:
                kwargs["creationflags"] = rewritten
                _converted += 1
                _event("guard_converted", f"creationflags {flags:#010x} -> {rewritten:#010x}")
            super().__init__(*args, **kwargs)

    # repr() and tracebacks should not suddenly say "WindowlessPopen".
    WindowlessPopen.__name__ = original.__name__
    WindowlessPopen.__qualname__ = original.__qualname__
    WindowlessPopen.__doc__ = original.__doc__
    WindowlessPopen.__module__ = original.__module__
    return WindowlessPopen


def install() -> bool:
    """Make every subprocess ABP starts windowless from now on. Safe to call more than once
    (a second call is a no-op, never a second wrapper); a no-op off Windows, where there is
    no console window to pop up. Returns whether the guard is in place."""
    global _original_popen, _wrapper_popen, _installed
    with _lock:
        if os.name != "nt":
            return False
        if _installed:
            return True
        _original_popen = subprocess.Popen
        _wrapper_popen = _make_wrapper(_original_popen)
        subprocess.Popen = _wrapper_popen    # type: ignore[misc, assignment]
        _installed = True
        return True


def uninstall() -> None:
    """Put the real Popen back (tests, and anything that needs to start a visible console
    without going through visible())."""
    global _original_popen, _wrapper_popen, _installed
    with _lock:
        if _installed and _original_popen is not None:
            subprocess.Popen = _original_popen    # type: ignore[misc]
        _installed = False
        _original_popen = None
        _wrapper_popen = None


def is_installed() -> bool:
    return _installed


def is_visible() -> bool:
    """True inside a `visible()` block."""
    return _visible_depth > 0


def converted() -> int:
    """How many spawns this process has had their creation flags rewritten - a real number
    for the diagnostics panel, and the honest answer to "is this still doing anything"."""
    return _converted


@contextlib.contextmanager
def visible() -> Iterator[None]:
    """Inside this block, a console a person can see is what was asked for: a spawn's
    `CREATE_NEW_CONSOLE` is kept as it is (and `DETACHED_PROCESS` becomes it). Nested
    blocks are counted, so an inner `with` cannot switch the guard off by accident."""
    global _visible_depth
    with _lock:
        _visible_depth += 1
    try:
        yield
    finally:
        with _lock:
            _visible_depth -= 1
