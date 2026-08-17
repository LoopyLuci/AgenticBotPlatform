"""Common interface every backend implements.

The router (bot/router.py) only ever calls .ask() and never needs to know
which backend it's talking to — adding a fourth backend later is a new
file implementing this class, not a change to the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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
