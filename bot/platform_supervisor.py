"""Owns the instance_id -> asyncio.Task mapping for every running bot.

Where bot/main.py used to build exactly one Telegram Application and start
Discord/Slack as (at most) one task each, this module makes "how many bots
are running, of which platform, on which backend" a dynamic set driven by
bot_instances rows instead of three hardcoded singletons — the same
asyncio-task-per-connection shape as before, just one per *instance* now,
so a Claude bot and a Hermes bot on the same platform run side by side.

Each platform's actual connection logic still lives where it always did
(bot/platforms/discord_platform.py, slack_platform.py, and Telegram's
build function in bot/main.py) — this module only supervises: start,
stop, restart, and status, recording last_error/last_started_at back onto
the bot_instances row so the dashboard's Bots tab can show "crashed 2 min
ago: <reason>" instead of just a static enabled/disabled flag.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from bot import bot_instances

logger = logging.getLogger("bot.platform_supervisor")


@dataclass
class _Handle:
    instance_id: int
    name: str
    platform: str
    task: asyncio.Task
    started_at: str = ""
    error: Optional[str] = None


_handles: dict[int, _Handle] = {}


def _build_credentials_set(row: dict[str, Any]) -> Any:
    """allowed_user_ids as the right type for each platform's comparisons —
    Telegram/Discord compare against ints, Slack against strings."""
    ids = row["allowed_user_ids"]
    if row["platform"] == "slack":
        return {str(i) for i in ids}
    return {int(i) for i in ids}


async def _run_discord(row: dict[str, Any]) -> None:
    from bot.platforms.discord_platform import DiscordPlatformInstance

    instance = DiscordPlatformInstance(
        instance_id=row["id"], name=row["name"],
        bot_token=row["credentials"]["bot_token"], allowed_ids=_build_credentials_set(row),
    )
    await instance.start()  # runs until cancelled


async def _run_slack(row: dict[str, Any]) -> None:
    from bot.platforms.slack_platform import SlackPlatformInstance

    instance = SlackPlatformInstance(
        instance_id=row["id"], name=row["name"],
        bot_token=row["credentials"]["bot_token"], app_token=row["credentials"].get("app_token", ""),
        allowed_ids=_build_credentials_set(row),
    )
    await instance.start()  # runs until cancelled


async def _run_telegram(row: dict[str, Any]) -> None:
    from bot.main import build_telegram_instance

    application = await build_telegram_instance(row)
    try:
        await asyncio.Event().wait()  # block until this task is cancelled
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


_RUNNERS = {"discord": _run_discord, "slack": _run_slack, "telegram": _run_telegram}


def _done_callback(instance_id: int, name: str) -> Any:
    def _cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("bot instance %r (id=%s) ended with an error: %s", name, instance_id, exc, exc_info=exc)
            bot_instances.record_error(instance_id, str(exc))
        handle = _handles.get(instance_id)
        if handle is not None and handle.task is task:
            _handles.pop(instance_id, None)

    return _cb


async def start_instance(row: dict[str, Any]) -> None:
    instance_id = row["id"]
    if instance_id in _handles:
        return  # already running
    runner = _RUNNERS.get(row["platform"])
    if runner is None:
        raise ValueError(f"unknown platform {row['platform']!r}")
    task = asyncio.create_task(runner(row))
    task.add_done_callback(_done_callback(instance_id, row["name"]))
    _handles[instance_id] = _Handle(instance_id=instance_id, name=row["name"], platform=row["platform"], task=task)
    bot_instances.record_start(instance_id)
    logger.info("started bot instance %r (id=%s, platform=%s)", row["name"], instance_id, row["platform"])


async def stop_instance(instance_id: int) -> None:
    handle = _handles.pop(instance_id, None)
    if handle is None:
        return
    handle.task.cancel()
    try:
        await handle.task
    except (asyncio.CancelledError, Exception):
        pass


async def restart_instance(instance_id: int) -> None:
    await stop_instance(instance_id)
    row = bot_instances.get_instance(instance_id)
    if row and row["enabled"]:
        await start_instance(row)


async def start_all_enabled(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        try:
            await start_instance(row)
        except Exception as exc:
            logger.error("failed to start bot instance %r (id=%s): %s", row["name"], row["id"], exc)
            bot_instances.record_error(row["id"], str(exc))


async def stop_all() -> None:
    for instance_id in list(_handles.keys()):
        await stop_instance(instance_id)


def status() -> dict[int, dict[str, Any]]:
    return {
        instance_id: {"running": not handle.task.done(), "platform": handle.platform, "name": handle.name}
        for instance_id, handle in _handles.items()
    }


def is_running(instance_id: int) -> bool:
    handle = _handles.get(instance_id)
    return handle is not None and not handle.task.done()
