"""Coordination: one work board so ABP's agents never race, duplicate work, sit idle or lose track of who does what.

`get_board()` returns the board to use. With `coordination.backend: auto` (default) that is AgentSentinel's machine-wide
board when its API answers (the answer is cached briefly), else a LocalBoard in ABP's own data folder, so ABP stays
fully standalone. `sentinel` and `local` force one. See docs/coordination.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Optional

from bot.coordination.base import Board
from bot.coordination.local import LocalBoard, default_path
from bot.coordination.model import BoardError, Unavailable, conflicts
from bot.coordination.sentinel import SentinelBoard
from bot.coordination.session import WorkLost, WorkSession, current_work

__all__ = ["Board", "BoardError", "LocalBoard", "SentinelBoard", "Unavailable", "WorkLost", "WorkSession",
           "aget_board", "local_board", "conflicts", "current_work", "enabled", "get_board", "reset", "set_board", "settings"]

logger = logging.getLogger("bot.coordination")

PROBE_TTL_S = 15.0

_lock = threading.Lock()
_override: Optional[Board] = None
_locals: dict[str, LocalBoard] = {}
_sentinel: Optional[SentinelBoard] = None
_probe: tuple[float, bool] = (0.0, False)


def settings() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        from bot.config import config

        cfg = dict(config.current.get("coordination") or {})
    except Exception:  # noqa: BLE001 - config trouble must not decide whether agents can coordinate
        pass
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "backend": os.environ.get("ABP_COORDINATION_BACKEND", "").strip() or str(cfg.get("backend") or "auto"),
        "lease_s": float(cfg.get("lease_s") or 120),
        "file_lock_lease_s": float(cfg.get("file_lock_lease_s") or 60),
        "swarm_workers": int(cfg.get("swarm_workers") or 8),
        "sentinel_url": cfg.get("sentinel_url") or "",
    }


def enabled() -> bool:
    return _override is not None or settings()["enabled"]


def set_board(board: Optional[Board]) -> None:
    """Force a board (tests, embedding); None goes back to the configured choice."""
    global _override
    _override = board


def reset() -> None:
    """Forget cached boards and the sentinel probe (tests, a config change)."""
    global _override, _sentinel, _probe
    with _lock:
        _override = None
        for b in _locals.values():
            b.close()
        _locals.clear()
        if _sentinel is not None:
            _sentinel.close()
        _sentinel = None
        _probe = (0.0, False)


def local_board() -> LocalBoard:
    path = str(default_path())
    with _lock:
        board = _locals.get(path)
        if board is None:
            board = _locals[path] = LocalBoard(path)
        return board


def _sentinel_board() -> SentinelBoard:
    global _sentinel
    with _lock:
        if _sentinel is None:
            from bot.coordination.sentinel import sentinel_token, sentinel_url

            _sentinel = SentinelBoard(sentinel_url(), sentinel_token(), timeout=10.0)
        return _sentinel


def _sentinel_answers() -> bool:
    global _probe
    stamp, ok = _probe
    if time.monotonic() - stamp < PROBE_TTL_S:
        return ok
    probe = SentinelBoard(_sentinel_board().url, _sentinel_board().token, timeout=1.5)
    try:
        ok = probe.ping()
    finally:
        probe.close()
    _probe = (time.monotonic(), ok)
    return ok


def get_board() -> Board:
    if _override is not None:
        return _override
    mode = settings()["backend"]
    if mode == "local":
        return local_board()
    if mode == "sentinel":
        return _sentinel_board()
    return _sentinel_board() if _sentinel_answers() else local_board()


async def aget_board() -> Board:
    """get_board() for async callers: the probe is a network call, so it runs in a thread when it is due."""
    if _override is not None or settings()["backend"] != "auto" or time.monotonic() - _probe[0] < PROBE_TTL_S:
        return get_board()
    return await asyncio.to_thread(get_board)
