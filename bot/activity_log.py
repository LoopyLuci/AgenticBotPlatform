"""A ring buffer over every log record this process emits — the backing
store for the GUI's Activity tab (bot/dashboard/server.py's /api/activity
route and the "activity_entry" WebSocket broadcast).

Deliberately not a new, separate logging call site sprinkled through the
codebase: bot/main.py's setup_logging() already routes every module's
logger through the root logger (that's how logs/bot.log gets everything
today), so attaching one more Handler there captures "every process and
activity or task" — jobs, bot actions, errors, warnings — for free,
without the parallel-logging-systems drift that comes from asking every
call site to also remember to emit a second, separate event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

_MAX_ENTRIES = 2000


@dataclass
class ActivityEntry:
    id: int
    ts: float
    level: str
    logger: str
    message: str


class _RingBufferHandler(logging.Handler):
    def __init__(self, maxlen: int = _MAX_ENTRIES) -> None:
        super().__init__()
        self._buf: deque[ActivityEntry] = deque(maxlen=maxlen)
        self._next_id = 1
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[ActivityEntry], None]] = []
        # Re-entrancy guard: this handler is attached to the ROOT logger, so
        # anything a subscriber does that itself logs — even indirectly,
        # even at WARNING/ERROR — comes right back through this same
        # emit() on the same thread. Confirmed live: bot.dashboard.server's
        # _on_activity_entry falls back to logger.warning(...) when called
        # outside a running event loop, which re-enters here, re-notifies
        # subscribers, warns again, and so on — a real, deterministic
        # infinite-recursion crash (eventually a RecursionError, logged as
        # "--- Logging error ---", or a hard native stack-overflow crash
        # with zero output before it, depending on exactly where the C
        # stack finally gives out) rather than a hypothetical one. A
        # thread-local flag means this can't happen no matter what a
        # current or future subscriber does inside its callback — the
        # record itself is still buffered either way, only the live
        # subscriber notification for a record logged *during* another
        # notification is skipped.
        self._dispatching = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = ActivityEntry(
                id=0,
                ts=record.created,
                level=record.levelname,
                logger=record.name,
                message=self.format(record),
            )
        except Exception:
            return
        callbacks: list[Callable[[ActivityEntry], None]] = []
        with self._lock:
            entry.id = self._next_id
            self._next_id += 1
            self._buf.append(entry)
            if not getattr(self._dispatching, "active", False):
                callbacks = list(self._subscribers)
        if not callbacks:
            return
        self._dispatching.active = True
        try:
            for cb in callbacks:
                try:
                    cb(entry)
                except Exception:
                    pass  # a broken subscriber must never take down logging itself
        finally:
            self._dispatching.active = False

    def recent(self, limit: int = 200, since_id: int = 0) -> list[ActivityEntry]:
        with self._lock:
            items = [e for e in self._buf if e.id > since_id]
        return items[-limit:]

    def subscribe(self, callback: Callable[[ActivityEntry], None]) -> Callable[[], None]:
        """Registers `callback` for every new entry from here on; returns
        an unsubscribe function. Used by the dashboard's WebSocket handler
        to forward live entries to connected GUIs — see server.py."""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe


_handler: Optional[_RingBufferHandler] = None


def install(level: int = logging.INFO) -> _RingBufferHandler:
    """Idempotent — safe to call more than once (returns the existing
    handler instead of attaching a second copy), since bot/main.py's
    module-level setup runs once but tests/tools may import it again."""
    global _handler
    if _handler is not None:
        return _handler
    _handler = _RingBufferHandler()
    _handler.setLevel(level)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> Optional[_RingBufferHandler]:
    return _handler


def recent(limit: int = 200, since_id: int = 0) -> list[dict]:
    if _handler is None:
        return []
    return [e.__dict__ for e in _handler.recent(limit=limit, since_id=since_id)]


def subscribe(callback: Callable[[ActivityEntry], None]) -> Callable[[], None]:
    if _handler is None:
        install()
    return _handler.subscribe(callback)  # type: ignore[union-attr]
