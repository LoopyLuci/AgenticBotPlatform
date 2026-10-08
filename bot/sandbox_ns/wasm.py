"""A `wasm` cell: one plugin tool call, run in the engine instead of in a process.

Every other cell in this package holds processes, because that is what a sandbox was until a
WebAssembly plugin turned up: a tool call was a Python interpreter, 30-50 MB and 100 ms of
start-up each, and the 6,000-process incident was what that adds up to. A wasm plugin needs
none of that - it is code inside ABP's own address space, started in microseconds and using a
few MB - so a cell for it is not a container. It is the *accounting*: one call, one cell, the
deadline and memory cap the engine enforces, a record for `data/sandbox_ns/live.json` and the
status page, and a kill that stops the call rather than a process tree.

What that buys, concretely:

    with cell_for("wasm", name="hello.greet", owner="agent:3") as cell:
        result = cell.invoke("hello", "greet", {"name": "world"})

`cell.pids()` is empty - there is no process - so `status()` says `"wasm"` for `os` and puts
the call's own numbers where the process ones would be. A cell that claimed to contain a
process tree it does not have would be exactly the kind of lie this package refuses to tell
(see cell.py).

**The limits are the engine's, not this module's.** The deadline is epoch interruption inside
wasmtime and the memory cap is a store limiter; this cell passes them in, times the call, and
records what came back. What it does *not* do is widen them: a caller can ask for a shorter
deadline than the plugin's own, never a longer one.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from bot.sandbox_ns import policy as policy_mod
from bot.sandbox_ns.policy import Policy

logger = logging.getLogger("bot.sandbox_ns.wasm")

#: The `wasm` preset. A wasm plugin runs in ABP's own process, so there is no process tree to
#: contain, no job object and no cgroup: the limits that matter are the ones the engine
#: enforces (the deadline, the memory cap), and the preset exists so `cell_for("wasm", ...)`
#: reads like every other cell and so the cap has somewhere to be written down. Below-normal
#: priority is not applicable (a thread's priority is ABP's, and a plugin cannot be given its
#: own), so this preset says only what is true.
PRESET: Policy = policy_mod.PRESETS["wasm"]


class WasmCell:
    """One plugin tool call, as a sandbox: accounted for, limited, and closable.

    Deliberately not a `Cell`: a `Cell` creates a Win32 job object or a cgroup, and there is
    no process for either to contain. What a wasm call needs is a record and a deadline, and
    pretending otherwise would mean a status page claiming a containment that is not there.
    """

    def __init__(
        self,
        policy: Optional[Policy] = None,
        *,
        plugin: str = "",
        tool: str = "",
        owner: str = "",
        timeout_ms: Optional[int] = None,
        memory_mb: Optional[int] = None,
    ) -> None:
        self.policy = policy_mod.with_defaults(policy or PRESET)
        self.plugin = plugin
        self.tool = tool
        self.owner = owner
        self.name = f"wasm:{plugin}.{tool}" if plugin else "wasm"
        self.created = time.time()
        self.notes: list = []
        self.calls = 0
        self.kills = 0
        self._closed = False
        # A cell may only ever be *stricter* than the plugin's own limits: the deadline here
        # is a ceiling on the ceiling, and taking the minimum of the two is what makes that
        # true. `timeout_ms`/`memory_mb` of 0 or None mean "whatever the plugin has".
        self._timeout_ms = timeout_ms or 0
        self._memory_mb = memory_mb or 0

    # ---- the call ------------------------------------------------------------------------------

    def invoke(self, plugin: str, tool: str, tool_input: Any = None, *, timeout_ms: Optional[int] = None) -> str:
        """Run one tool on a loaded plugin and return its result as text.

        The plugin must already be loaded (`bot.wasm_plugins.manager`); this is the accounting
        around one call, not the loading. The deadline used is the smaller of the cell's and
        the plugin's, so a caller can shorten a call but never lengthen it.
        """
        if self._closed:
            raise RuntimeError("this wasm cell is closed")
        from bot.wasm_plugins import manager
        from bot.wasm_plugins import runtime as rt

        budget = min(
            [x for x in (self._timeout_ms, timeout_ms, self.policy.timeout_s and int(self.policy.timeout_s * 1000))
             if x]
            or [rt.DEFAULT_TIMEOUT_MS]
        )
        budget = max(1, min(int(budget), rt.MAX_TIMEOUT_MS))
        started = time.perf_counter()
        self.calls += 1
        try:
            return manager.invoke(plugin, tool, tool_input, timeout_ms=budget)
        except rt.PluginCallError as exc:
            # The engine stopped it: the deadline or the memory cap. That is this cell doing
            # its job, so it is counted as a kill rather than left as an unexplained failure.
            self.kills += 1
            self.notes.append(f"{tool}: {exc}")
            raise
        except Exception as exc:  # noqa: BLE001 - a trap is a failed call, and it is recorded
            self.notes.append(f"{tool}: {exc}")
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            logger.debug("wasm cell %s: %s.%s took %.1fms", self.name, plugin, tool, elapsed)

    # ---- the cell interface (registry, status, closing) ------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def pids(self) -> list:
        """Empty, and that is the truth: a wasm call has no process."""
        return []

    def status(self) -> dict:
        """What the diagnostics view and the sandbox page show for this cell.

        `os` is `"wasm"` rather than `"windows"`/`"posix"` because neither is what this is,
        and the limits reported are the ones the engine enforces, named as such."""
        return {
            "name": self.name,
            "kind": "wasm",
            "owner": self.owner,
            "plugin": self.plugin,
            "tool": self.tool,
            "created": round(self.created, 3),
            "closed": self._closed,
            "pids": [],
            "kills": self.kills,
            "calls": self.calls,
            "notes": list(self.notes),
            "os": "wasm",
            "limits_applied": {
                "timeout_ms": self._timeout_ms or None,
                "memory_mb": self._memory_mb or None,
                "enforced_by": "wasmtime (epoch deadline, store limiter)",
            },
        }

    def close(self) -> None:
        self._closed = True

    def kill(self, reason: str = "") -> int:
        """Nothing to kill: the call either finished or was stopped by the engine's deadline,
        which is counted in `invoke`. Closing is here so a `wasm` cell is interchangeable with
        any other in a `finally`."""
        self._closed = True
        return 0

    def __enter__(self) -> "WasmCell":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<WasmCell {self.name} calls={self.calls} closed={self._closed}>"


def new_cell(
    *,
    plugin: str = "",
    tool: str = "",
    owner: str = "",
    timeout_ms: Optional[int] = None,
    memory_mb: Optional[int] = None,
    policy: Optional[Policy] = None,
) -> WasmCell:
    """`new_cell(plugin="hello", tool="greet")` - the wasm equivalent of
    bot.sandbox_ns.cell.new_cell."""
    return WasmCell(
        policy,
        plugin=plugin,
        tool=tool,
        owner=owner,
        timeout_ms=timeout_ms,
        memory_mb=memory_mb,
    )


def describe_policy() -> dict:
    """The `wasm` preset as the policy page shows it."""
    return PRESET.describe()


def run(plugin: str, tool: str, tool_input: Any = None, *, timeout_ms: Optional[int] = None,
        owner: str = "") -> dict:
    """One call, wrapped in a cell: the result and how long it took.

    The one-call form, for a caller that wants the accounting rather than the cell. It exists
    because that is nearly every call site, and a caller should not have to open a context
    manager to get a number in the log."""
    cell = new_cell(plugin=plugin, tool=tool, owner=owner, timeout_ms=timeout_ms)
    started = time.perf_counter()
    try:
        result = cell.invoke(plugin, tool, tool_input)
        return {"ok": True, "result": result, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 - the caller decides what to do with a failure
        return {"ok": False, "error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    finally:
        cell.close()


__all__ = ["PRESET", "WasmCell", "new_cell", "describe_policy", "run"]
