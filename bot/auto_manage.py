"""Auto-management: a manager-persona bot instance can be configured to
autonomously check in and organize work — via a scheduled interval, a
reactive trigger (a new kanban card appearing), or both, per the user's
explicit choice ("both, configurable per instance").

Stored in bot_instances.action_overrides["auto_manage"] (the established
growth point for this kind of per-instance JSON extension — no new
column needed): {"enabled": bool, "trigger": "scheduled"|
"kanban_card_created"|"both", "interval": "30m", "chat_id": ...,
"thread_id": ..., "goal_template": str}.

Both triggers funnel through run_check_in() — the one place this
capability's actual behavior lives, dispatched through the same
agent-loop engine (bot/agent_runtime/engine.py) every other prompt goes
through (identical tool access, approval gating, session history — not a
separate, weaker execution path), mirroring bot/scheduler.py's own
_fire()'s exact background-dispatch-plus-deliver-to-chat shape. The
manager itself then uses its own already-existing delegation tools
(delegate_to_instance, spawn_subagent, dispatch_native_swarm_goal/
dispatch_swarm_goal) to actually organize work — this module adds no new
dispatch mechanism of its own.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("bot.auto_manage")

VALID_TRIGGERS = ("scheduled", "kanban_card_created", "both")

DEFAULT_GOAL_TEMPLATE = (
    "Auto-management check-in ({reason}). Review what needs doing and use your own delegation tools "
    "(delegate_to_instance, spawn_subagent, dispatch_native_swarm_goal/dispatch_swarm_goal) to organize "
    "and advance the work."
)


class AutoManageError(Exception):
    pass


def get_config(instance_id: int) -> dict[str, Any]:
    from bot import bot_instances

    instance = bot_instances.get_instance(instance_id)
    if instance is None:
        raise AutoManageError(f"instance {instance_id} not found")
    return dict((instance.get("action_overrides") or {}).get("auto_manage") or {"enabled": False})


def set_config(instance_id: int, *, actor: str = "dashboard", **fields: Any) -> dict[str, Any]:
    """Merges the given fields into this instance's stored auto_manage
    config, creating it if it doesn't exist yet. Every keyword actually
    passed is applied literally, including an explicit None (needed by
    disable() to genuinely clear schedule_id, not merely skip it) —
    callers control what's included via **fields, so there is no
    separate "omitted vs. explicitly None" ambiguity to resolve here."""
    from bot import bot_instances

    instance = bot_instances.get_instance(instance_id)
    if instance is None:
        raise AutoManageError(f"instance {instance_id} not found")
    if "trigger" in fields and fields["trigger"] is not None and fields["trigger"] not in VALID_TRIGGERS:
        raise AutoManageError(f"trigger must be one of {VALID_TRIGGERS}")

    overrides = dict(instance.get("action_overrides") or {})
    current = dict(overrides.get("auto_manage") or {})
    current.update(fields)
    overrides["auto_manage"] = current
    bot_instances.update_instance(instance_id, actor=actor, action_overrides=overrides)
    return current


def enable(
    instance_id: int,
    *,
    chat_id: Any,
    thread_id: Any = None,
    trigger: str = "scheduled",
    interval: str = "30m",
    goal_template: Optional[str] = None,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Turns auto-management on for this instance. When [trigger] is
    "scheduled" or "both", creates the real underlying scheduled_commands
    row (via bot/scheduler.py, kind="auto_manage") that actually drives
    the periodic check-in — its id is stored in the config so a later
    disable()/re-enable() can find and remove/replace it rather than
    leaking a schedule row every time settings change."""
    from bot import scheduler

    if trigger not in VALID_TRIGGERS:
        raise AutoManageError(f"trigger must be one of {VALID_TRIGGERS}")

    existing = get_config(instance_id)
    old_schedule_id = existing.get("schedule_id")
    if old_schedule_id:
        try:
            scheduler.remove(old_schedule_id)
        except Exception:
            logger.warning("auto_manage.enable: could not remove stale schedule %s for instance %s", old_schedule_id, instance_id)

    schedule_id = None
    if trigger in ("scheduled", "both"):
        try:
            interval_s = scheduler.parse_duration(interval)
        except scheduler.ScheduleError as exc:
            raise AutoManageError(str(exc))
        schedule_id = scheduler.create(instance_id, chat_id, "auto_manage", "", interval_s, thread_id=thread_id)

    return set_config(
        instance_id, actor=actor, enabled=True, trigger=trigger, interval=interval,
        chat_id=chat_id, thread_id=thread_id,
        goal_template=goal_template if goal_template is not None else existing.get("goal_template"),
        schedule_id=schedule_id,
    )


def disable(instance_id: int, *, actor: str = "dashboard") -> dict[str, Any]:
    """Turns auto-management off and removes its underlying schedule row
    (if any) — a disabled auto_manage never leaves an orphaned recurring
    schedule silently still firing (it would no-op via run_check_in's own
    enabled check, but removing it outright is cleaner and matches what
    a user would expect "disable" to actually do)."""
    from bot import scheduler

    existing = get_config(instance_id)
    schedule_id = existing.get("schedule_id")
    if schedule_id:
        try:
            scheduler.remove(schedule_id)
        except Exception:
            logger.warning("auto_manage.disable: could not remove schedule %s for instance %s", schedule_id, instance_id)
    return set_config(instance_id, actor=actor, enabled=False, schedule_id=None)


def _matches_trigger(cfg: dict[str, Any], trigger: str) -> bool:
    configured = cfg.get("trigger") or "scheduled"
    return configured == trigger or configured == "both"


async def run_check_in(instance_id: int, reason: str) -> None:
    """The one place a check-in's actual behavior lives — called by both
    the scheduled trigger (bot/scheduler.py dispatches a kind="auto_manage"
    row straight through agent_engine.run_turn like any other schedule)
    and the reactive trigger (see maybe_trigger_from_kanban_card below),
    so they can never diverge in what they actually do. A no-op (not an
    error) if auto_manage isn't enabled or has no chat_id configured —
    callers don't need to pre-check either."""
    from bot import bot_instances, db, outbox
    from bot.agent_runtime import engine as agent_engine

    try:
        cfg = get_config(instance_id)
    except AutoManageError:
        return
    if not cfg.get("enabled"):
        return
    chat_id = cfg.get("chat_id")
    if chat_id is None:
        logger.warning("auto_manage is enabled for instance %s but has no chat_id configured — skipping check-in", instance_id)
        return
    if bot_instances.get_instance(instance_id) is None:
        return

    thread_id = cfg.get("thread_id")
    goal = (cfg.get("goal_template") or DEFAULT_GOAL_TEMPLATE).format(reason=reason)

    async def _deliver(outcome: str, result) -> None:
        if outcome != "ran":
            return
        try:
            await outbox.send_message(instance_id, chat_id, f"🧭 {result.text}", thread_id=thread_id)
        except RuntimeError:
            pass

    await agent_engine.run_turn(
        goal, action_type="auto_manage", user_id=0, instance_id=instance_id,
        chat_id=chat_id, thread_id=thread_id, background=True, on_result=_deliver,
    )
    db.log_audit(actor="auto-manage", action="auto_manage_check_in", detail=f"instance {instance_id}: {reason}")


async def maybe_trigger_from_kanban_card(card_id: int) -> None:
    """The reactive half — registered as a db.on_kanban_card_created()
    listener (see bot/main.py's startup wiring). Resolves the card's
    owning instance and fires a real check-in only if that instance's
    auto_manage config actually opts into the "kanban_card_created"
    trigger (or "both") — every other instance's cards are silently
    ignored, not just skipped with a warning, since this fires on EVERY
    card creation across the whole app."""
    from bot import db

    card = db.get_kanban_card(card_id)
    if card is None:
        return
    board = db.get_kanban_board(card["board_id"])
    if board is None:
        return
    instance_id = board["instance_id"]
    try:
        cfg = get_config(instance_id)
    except AutoManageError:
        return
    if not cfg.get("enabled") or not _matches_trigger(cfg, "kanban_card_created"):
        return
    await run_check_in(instance_id, reason=f"new kanban card #{card_id}")
