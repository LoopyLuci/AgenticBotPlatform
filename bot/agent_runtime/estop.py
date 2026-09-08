"""Global emergency stop — mirrors Hermes Agent's own estop.py concept
(confirmed against its real source: a global pause sentinel checked by
long-running components like cron and its gateway before starting new
work). DB-backed here (a single-row `estop_state` table) instead of a
sentinel file, since BotServer already centralizes runtime state in
SQLite rather than the filesystem.

Checked at the top of every new-turn/new-dispatch entry point —
`NativeAgentBackend.ask()`, `subagents.run_batch()`,
`scheduler._fire()`, `auto_manage.run_check_in()` — never mid-turn: an
already-running turn finishes rather than being killed, matching
Hermes's own "checked before new work" semantics exactly.
"""

from __future__ import annotations

from typing import Optional

from bot.backends.base import BackendError


class EstopEngagedError(BackendError):
    """Raised by a new-work entry point when the emergency stop is
    engaged — callers should surface this message as-is, not retry. A
    BackendError subclass so it's caught by every existing
    `except BackendError` handler throughout the codebase without each
    one needing a separate case for this."""


def is_engaged() -> bool:
    from bot import db

    return db.get_estop_state()["engaged"]


def status() -> dict:
    from bot import db

    return db.get_estop_state()


def engage(reason: Optional[str] = None, actor: str = "dashboard") -> dict:
    from bot import db

    db.set_estop_state(True, reason, actor)
    db.log_audit(actor=actor, action="estop_engage", detail=reason or "")
    return status()


def disengage(actor: str = "dashboard") -> dict:
    from bot import db

    db.set_estop_state(False, None, actor)
    db.log_audit(actor=actor, action="estop_disengage", detail="")
    return status()


def check() -> None:
    """Raises EstopEngagedError if engaged — the one call every new-work
    entry point makes at its very start."""
    state = status()
    if state["engaged"]:
        reason = f" ({state['reason']})" if state["reason"] else ""
        raise EstopEngagedError(f"emergency stop is engaged{reason} — no new work will start until disengaged")
