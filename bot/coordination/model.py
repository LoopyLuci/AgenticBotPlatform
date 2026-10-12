"""Shared pieces of the work board: errors, states, the resource overlap rule, one clock.

The board protocol itself is described in docs/coordination.md; every name here (error codes, states, field names)
is the wire shape of AgentSentinel's /api/v1/board, so a LocalBoard and a SentinelBoard answer the same way.
"""
from __future__ import annotations

import time
from typing import Any, Optional

STATES = ("open", "claimed", "running", "review", "done", "failed", "cancelled")
TERMINAL = frozenset({"done", "failed", "cancelled"})
ACTIVE = frozenset({"claimed", "running", "review"})
SETTABLE = frozenset({"running", "review", "done", "failed", "released"})

DEFAULT_LEASE_S = 120.0
MIN_LEASE_S = 5.0
MAX_LEASE_S = 3600.0
HEARTBEAT_INTERVAL_S = 15.0       # presence: an agent is gone after three of these without a sign of life
MAX_DETAIL = 16 * 1024
MAX_RESULT = 64 * 1024
MAX_TITLE = 300
MAX_RESOURCES = 64


class BoardError(Exception):
    """A refusal from the board. `code` is the protocol's (TAKEN, LOCKED, DEPS, VERSION, STALE_FENCE, NOT_OWNER,
    NOT_FOUND, BAD_REQUEST, UNAVAILABLE); `holders` lists who holds the thing for LOCKED."""

    def __init__(self, code: str, message: str = "", *, status: int = 409, holders: Optional[list[dict]] = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status
        self.holders = holders or []

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message != self.code else self.code


class Unavailable(BoardError):
    """The board could not be reached at all (as opposed to refusing)."""

    def __init__(self, message: str = "board unreachable"):
        super().__init__("UNAVAILABLE", message, status=503)


def now_ms() -> int:
    return int(time.time() * 1000)


def split_path(resource: str) -> Optional[tuple[str, str]]:
    """('repo', 'relative/path') for a path: resource, else None."""
    if not resource.startswith("path:"):
        return None
    repo, _, rel = resource[5:].partition(":")
    return repo, rel.replace("\\", "/").strip("/")


def conflicts(a: str, b: str) -> bool:
    """Do two resource strings overlap? Equal always; two path: resources of one repo when one is the other or a
    folder prefix of it (path:abp:bot vs path:abp:bot/db.py yes, path:abp:bot/db vs path:abp:bot/db.py no)."""
    if a == b:
        return True
    pa, pb = split_path(a), split_path(b)
    if pa is None or pb is None or pa[0] != pb[0]:
        return False
    x, y = pa[1], pb[1]
    if x == y or not x or not y:
        return True
    return x.startswith(y + "/") or y.startswith(x + "/")


def mode_conflicts(mode_a: str, mode_b: str) -> bool:
    return not (mode_a == "shared" and mode_b == "shared")


def norm_resources(items: Any) -> list[tuple[str, str]]:
    """[(resource, mode)] from strings, {resource, mode} dicts or pairs."""
    out: list[tuple[str, str]] = []
    for it in items or []:
        if isinstance(it, str):
            res, mode = it, "exclusive"
        elif isinstance(it, dict):
            res, mode = str(it.get("resource") or ""), str(it.get("mode") or "exclusive")
        else:
            res, mode = str(it[0]), str(it[1]) if len(it) > 1 else "exclusive"
        if not res:
            raise BoardError("BAD_REQUEST", "empty resource", status=400)
        if mode not in ("exclusive", "shared"):
            raise BoardError("BAD_REQUEST", f"bad lock mode {mode!r}", status=400)
        out.append((res, mode))
    if len(out) > MAX_RESOURCES:
        raise BoardError("BAD_REQUEST", f"at most {MAX_RESOURCES} resources per call", status=400)
    return out


def clamp_lease(lease_s: Optional[float], minimum: float = MIN_LEASE_S) -> float:
    if lease_s is None:
        return DEFAULT_LEASE_S
    return max(minimum, min(MAX_LEASE_S, float(lease_s)))
