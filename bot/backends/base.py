"""Common interface every backend implements.

The router (bot/router.py) only ever calls .ask() and never needs to know
which backend it's talking to — adding a fourth backend later is a new
file implementing this class, not a change to the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # the cell is only needed at call time, never at import time
    from bot.sandbox_ns.cell import Cell


@dataclass
class BackendResult:
    text: str
    tokens: Optional[int] = None
    raw: Optional[Any] = None


class BackendError(Exception):
    """Raised on any failure — timeout, process error, missing window, etc.
    The router catches this to decide whether to try the backup chain."""


class Backend:
    name: str = "base"

    async def ask(self, prompt: str, *, context: Optional[dict] = None, timeout_s: float = 30) -> BackendResult:
        raise NotImplementedError


def process_cell(name: str, *, preset: str = "agent", owner: str = "backends") -> "Cell":
    """The cell one backend process lives in (bot/sandbox_ns, the Sandbox Nervous System).

    Every CLI agent backend gets **one cell per run**, not one per process, because the CLI
    starts tool subprocesses of its own: `proc.kill()` stopped the client and left the tools
    running after a timeout or a /stop, and the cell is what makes one kill take the whole
    tree (a Win32 Job Object on Windows, a session of its own elsewhere). It also buys what a
    bare subprocess never had: the run is recorded, with its owner and its limits, so the
    diagnostics page can say which backend is busy. `preset="daemon"` for the one backend whose
    process is meant to keep running after ABP exits.

    Close it when the process is gone - that releases the job handle instead of leaving one
    open per run for the life of the process."""
    from bot.sandbox_ns.cell import new_cell

    return new_cell(preset, name=name, owner=owner)
