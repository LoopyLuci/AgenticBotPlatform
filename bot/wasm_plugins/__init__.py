"""WebAssembly plugins for ABP: sandboxed tools that start in microseconds.

A wasm plugin is a component that exports `abp:plugin/guest@0.1.0` (see `wit/`). It runs
inside a wasm engine inside ABP's own process, not in a subprocess: no 30 MB interpreter, no
100 ms of start-up, and no process to leak. What it can reach is decided per plugin and shown
to a person at install time - a deadline, a memory cap, a key-value store only it can see, and
the directories and hosts they approved and nothing else.

**The layout.** `runtime.py` is the native half and the capability decisions; `manager.py` is
install / enable / disable / uninstall / invoke and the audit log; `tools.py` turns a
plugin's tools into agent tools. `bot/sandbox_ns/wasm.py` runs one plugin call as a `wasm`
cell, so a plugin's memory and time are accounted for like any other sandbox's.

**Without the native module nothing here is imported.** `bot/wasm_plugins/runtime.py` reports
once that `abp_wasm` is missing and says how to build it, and every entry point refuses with
that message. `ABP_NATIVE=0` does the same deliberately, so both paths can be tested on one
build.

`bot/main.py` calls `wasm_plugins.load_enabled()` at start-up, next to its own
`plugins.load_enabled()`.
"""

from __future__ import annotations

from bot.wasm_plugins import manager, runtime, tools

__all__ = ["manager", "runtime", "tools", "available", "load_enabled"]


def available() -> bool:
    """Whether the WebAssembly feature can run at all. False means the native module is not
    built (or ABP_NATIVE=0), and the feature is off with a message rather than broken."""
    return runtime.available()


def load_enabled() -> None:
    """Load the plugins marked enabled. Called once at start-up."""
    if not runtime.available():
        return
    manager.load_enabled()
