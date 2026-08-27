"""Real recurring-prompt scheduler backing /cron, /loop, and /heartbeat —
one background asyncio task (started from bot/main.py alongside the
Telegram/dashboard tasks, stopped the same way on shutdown) that polls
bot/db.py's scheduled_commands table and dispatches each due row through
the same agent-loop engine /background uses, so a scheduled prompt gets
identical tool access, approval gating, and session history to a
manually-typed one — not a separate, weaker execution path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot import db, outbox

logger = logging.getLogger("bot.scheduler")

POLL_INTERVAL_S = 15

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ScheduleError(Exception):
    pass


def parse_duration(text: str) -> int:
    """"30s" / "10m" / "2h" / "1d" -> seconds. Bare integers are seconds."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = _DURATION_RE.match(text)
    if not m:
        raise ScheduleError(f"unrecognized interval {text!r} — use e.g. 30s, 10m, 2h, 1d")
    return int(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def create(
    instance_id: int, chat_id, kind: str, prompt: str, interval_s: int,
    max_runs: Optional[int] = None, thread_id=None,
) -> int:
    if interval_s < 5:
        raise ScheduleError("interval must be at least 5 seconds")
    next_run = _iso(_now() + timedelta(seconds=interval_s))
    return db.create_scheduled_command(instance_id, chat_id, kind, prompt, interval_s, next_run, max_runs, thread_id=thread_id)


def list_for_chat(instance_id: int, chat_id, thread_id=None) -> list[dict]:
    return [dict(r) for r in db.list_scheduled_commands(instance_id, chat_id=chat_id, thread_id=thread_id)]


def pause(sched_id: int) -> None:
    db.set_scheduled_command_enabled(sched_id, False)


def resume(sched_id: int) -> None:
    db.set_scheduled_command_enabled(sched_id, True)


def remove(sched_id: int) -> None:
    db.delete_scheduled_command(sched_id)


async def _fire(row) -> None:
    from bot import bot_instances
    from bot.agent_runtime import engine as agent_engine  # deferred: avoids an import cycle at module load

    instance_id, chat_id, thread_id = row["instance_id"], row["chat_id"], row["thread_id"]

    if bot_instances.get_instance(instance_id) is None:
        # The instance was deleted through some path that predates
        # delete_instance's own cleanup (a direct DB edit, a restore from
        # an old backup, or simply a row created before this check
        # existed) — nothing this schedule could ever run against again.
        # Disabling instead of deleting leaves a visible trace of what
        # happened rather than silently vanishing a row someone might
        # otherwise wonder about.
        logger.warning(
            "scheduled command %s targets instance %s, which no longer exists — disabling it",
            row["id"], instance_id,
        )
        db.set_scheduled_command_enabled(row["id"], False)
        return

    if row["kind"] == "heartbeat" and agent_engine.is_running(instance_id, chat_id, thread_id):
        # "re-enters the session when idle" — if it's busy right now, just
        # check back next poll instead of firing on top of it.
        return

    async def _deliver(outcome: str, result) -> None:
        if outcome != "ran":
            return
        try:
            await outbox.send_message(instance_id, chat_id, f"⏰ {result.text}", thread_id=thread_id)
        except RuntimeError:
            pass

    await agent_engine.run_turn(
        row["prompt"],
        action_type="scheduled",
        user_id=0,
        instance_id=instance_id,
        chat_id=chat_id,
        thread_id=thread_id,
        background=True,
        on_result=_deliver,
    )
    next_run = _iso(_now() + timedelta(seconds=row["interval_s"]))
    db.mark_scheduled_command_ran(row["id"], next_run)


async def run_forever(stop_event: asyncio.Event) -> None:
    logger.info("scheduler started (poll every %ss)", POLL_INTERVAL_S)
    while not stop_event.is_set():
        try:
            due = db.list_due_scheduled_commands(_iso(_now()))
            for row in due:
                try:
                    await _fire(row)
                except Exception:
                    logger.exception("scheduled command %s failed", row["id"])
        except Exception:
            logger.exception("scheduler poll failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    logger.info("scheduler stopped")
