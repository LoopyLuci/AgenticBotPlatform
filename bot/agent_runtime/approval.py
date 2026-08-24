"""Exec-approval gate for dangerous tool calls — the run_shell/write_file
pause-and-ask-a-human flow. Modeled on the real Hermes Agent's own
approval semantics (once/session/always/deny — confirmed by reading its
gateway/run.py and the Telegram adapter's ea: callback handling): a
resolved-once-only in-memory wait per pending call, with an "already
resolved" guard so a late button tap can never claim an outcome that
already timed out or was answered elsewhere.

The wait itself is a plain asyncio.Event living in this process's memory
(there's exactly one BotServer process, so that's sufficient — no need for
cross-process signaling); bot/db.py's pending_approvals table is the
durable/audit side a Telegram message edit or dashboard view reads back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from bot import db

logger = logging.getLogger("bot.agent_runtime.approval")

DEFAULT_TIMEOUT_S = 300  # same default Hermes uses — long enough for a push notification round trip

_waiters: dict[int, asyncio.Event] = {}
_outcomes: dict[int, str] = {}

Outcome = str  # "once" | "session" | "always" | "deny"


def is_pre_approved(instance_id: int, session_key: str, tool_name: str) -> bool:
    return db.has_tool_approval(instance_id, session_key, tool_name)


async def request_approval(
    instance_id: int,
    chat_id: Any,
    session_key: str,
    tool_name: str,
    tool_input: dict,
    notify: Callable[[int, str, dict], Awaitable[None]],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Outcome:
    """Blocks until a human resolves this call (or it times out, treated
    as a deny). `notify` is awaited once, synchronously, before the wait
    begins — it should send whatever a human sees (a Telegram message with
    ea: buttons) — so there's no race between the message existing and a
    tap on it trying to resolve an approval id that isn't registered yet."""
    if is_pre_approved(instance_id, session_key, tool_name):
        return "once"

    approval_id = db.create_pending_approval(instance_id, chat_id, session_key, tool_name, tool_input)
    event = asyncio.Event()
    _waiters[approval_id] = event
    try:
        await notify(approval_id, tool_name, tool_input)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            db.resolve_pending_approval(approval_id, status="expired", resolved_by=None)
            logger.info("approval %s (%s) expired after %ss", approval_id, tool_name, timeout_s)
            return "deny"
        outcome = _outcomes.pop(approval_id, "deny")
        if outcome == "session":
            db.grant_tool_approval(instance_id, tool_name, session_key=session_key)
        elif outcome == "always":
            db.grant_tool_approval(instance_id, tool_name, session_key=None)
        return outcome
    finally:
        _waiters.pop(approval_id, None)


def resolve(approval_id: int, outcome: Outcome, actor: str) -> bool:
    """Called from the ea: callback handler or /approve /deny. Returns
    False (no-op) if this approval was already resolved or doesn't exist —
    the caller should show "already resolved", never claim success twice."""
    event = _waiters.get(approval_id)
    if event is None:
        return False
    row = db.get_pending_approval(approval_id)
    if row is None or row["status"] != "pending":
        return False
    status = "denied" if outcome == "deny" else f"approved_{outcome}"
    db.resolve_pending_approval(approval_id, status=status, resolved_by=actor)
    _outcomes[approval_id] = outcome
    event.set()
    return True


def oldest_pending(instance_id: int, chat_id: Any) -> Optional[dict]:
    rows = db.list_pending_approvals(instance_id, chat_id=chat_id)
    return dict(rows[0]) if rows else None
