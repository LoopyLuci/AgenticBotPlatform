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

Telegram carries two extra rules here, both because a Telegram bot token's
`getUpdates` can have exactly ONE long-poller anywhere on the machine:

  * **Token ownership.** Before any Telegram instance starts polling, the
    runner asks bot/hermes_gateway.py whether that token is one a running
    Hermes gateway is already serving. If it is, ABP does not poll: the task
    parks in a re-check loop, the instance's status says "served by the Hermes
    gateway (<home>)", and — unless the instance opted in with
    `takeover_when_gateway_down` — it stays parked even after that gateway
    stops. Silently taking the token over is exactly the failure mode this
    whole arrangement exists to avoid.
  * **409 Conflict.** If something else polls the same token anyway,
    python-telegram-bot hands us `telegram.error.Conflict` on every poll. Its
    default is to log it and retry forever, backing off to 30 s — a permanent,
    useless retry loop that also knocks the other poller off in turn. This
    module instead stops that one instance, records the reason, and leaves it
    stopped until an operator starts it again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from bot import bot_instances
from bot import tasks as bg

logger = logging.getLogger("bot.platform_supervisor")

# A crashed instance restarts itself with exponential backoff instead of
# staying dead until someone notices and clicks restart — the same
# "recover automatically" treatment every other background loop in this
# app already gets. The attempt counter resets once an instance has gone
# long enough without crashing again that it's clearly not stuck in a
# tight failure loop.
_RESTART_RESET_AFTER_S = 300.0
_RESTART_MAX_BACKOFF_S = 60.0
_restart_state: dict[int, dict[str, float]] = {}

# How often a Telegram instance parked on a Hermes-owned token re-asks
# bot/hermes_gateway.py whether the gateway is still up. Long enough that a
# dashboard open on top of it costs nothing, short enough that stopping the
# gateway by hand takes effect in well under a minute.
GATEWAY_RECHECK_S = 30.0

# Set for a Telegram instance the moment python-telegram-bot reports a 409, so
# the runner's wait ends immediately instead of after a full polling backoff.
_conflict_events: dict[int, asyncio.Event] = {}

# A self-hosted Telegram Bot API server (or, in tests, a local fake one) for
# every Telegram instance: python-telegram-bot's own `base_url`, which it uses
# verbatim - so it must include the trailing "/bot", exactly like its default
# https://api.telegram.org/bot. Unset means the real api.telegram.org.
API_BASE_URL_ENV = "ABP_TELEGRAM_API_BASE_URL"


@dataclass
class _Handle:
    instance_id: int
    name: str
    platform: str
    task: asyncio.Task
    started_at: str = ""
    error: Optional[str] = None
    status: str = ""
    served_by: str = ""


_handles: dict[int, _Handle] = {}


def _build_credentials_set(row: dict[str, Any]) -> Any:
    """allowed_user_ids as the right type for each platform's comparisons —
    Telegram/Discord compare against ints, Slack/Matrix against strings
    (Slack member IDs and Matrix user IDs like @name:server are never
    numeric)."""
    ids = row["allowed_user_ids"]
    if row["platform"] in bot_instances.STRING_ID_PLATFORMS and row["platform"] not in ("telegram", "discord"):
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
    """Start polling one Telegram instance — unless somebody else already is.

    The ownership check happens here, before bot/main.py builds the PTB
    Application, so "ABP must not poll a Hermes-owned token" is enforced by
    never reaching the point where a poll is issued, rather than by starting a
    poller and hoping it wins.

    The park loop below has two shapes, and which one it takes is the whole
    point of `takeover_when_gateway_down`:

      * **default (false).** Once this instance has seen a running Hermes
        gateway own its token, it never polls it — not even after that gateway
        stops. It keeps re-checking, so a gateway that comes back is reflected
        in the status, but the answer stays no.
      * **opted in (true).** It parks only while the gateway is up and polls
        the moment the gateway is gone."""
    from bot import hermes_gateway

    # Clear a 409 left over from a PREVIOUS run of this instance before anything
    # waits on it. The event is only popped by stop_instance(), so a set one
    # would still be set when this run parks on a Hermes-owned token — and
    # _sleep_or_conflict() would return instantly, spinning this loop (and the
    # ownership re-check it drives) as fast as the event loop can go.
    _conflict_events.setdefault(row["id"], asyncio.Event()).clear()

    takeover = bool(row.get("takeover_when_gateway_down"))
    owner = hermes_gateway.instance_owner(row)
    while owner is not None and (owner.running or not takeover):
        if owner.running:
            _set_status(row["id"], owner.status_text, served_by=owner.status_text)
            logger.info("not polling Telegram instance %r (id=%s): %s", row["name"], row["id"], owner.status_text)
        else:
            # Deliberately NOT record_error()'d: last_error is the dashboard's
            # "Crashed: <reason>" field, and this instance is not crashed - it is
            # being left alone on purpose. Say why here instead, so the card
            # does not read as a bot that merely needs pressing Start.
            _set_status(row["id"], hermes_gateway.GATEWAY_DOWN_TEMPLATE.format(home=owner.home))
            logger.info(
                "the Hermes gateway serving Telegram instance %r (id=%s) stopped, and this instance did not "
                "opt in to takeover_when_gateway_down - staying idle rather than taking the token over",
                row["name"], row["id"],
            )
        if owner.running and takeover:
            logger.info(
                "instance %r (id=%s) has takeover_when_gateway_down set - it will start polling "
                "as soon as that gateway is gone", row["name"], row["id"],
            )
        await _sleep_or_conflict(row["id"], GATEWAY_RECHECK_S)
        owner = hermes_gateway.instance_owner(row)

    from bot.main import build_telegram_instance

    application = await build_telegram_instance(
        row,
        api_base_url=os.environ.get(API_BASE_URL_ENV, "").strip(),
        polling_error_callback=_telegram_error_callback(row),
    )
    try:
        await _wait_or_conflict(row["id"])
    finally:
        _stop_polling_task(application)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def _run_matrix(row: dict[str, Any]) -> None:
    from bot.platforms.matrix_platform import MatrixPlatformInstance

    creds = row["credentials"]
    instance = MatrixPlatformInstance(
        instance_id=row["id"], name=row["name"],
        homeserver=creds["homeserver"], user_id=creds["user_id"],
        access_token=creds["access_token"], device_id=creds.get("device_id") or "",
        allowed_ids=_build_credentials_set(row),
    )
    await instance.start()  # runs until cancelled


async def _run_whatsapp(row: dict[str, Any]) -> None:
    from bot.platforms.whatsapp_platform import run_instance

    await run_instance(row)  # runs until cancelled


def _runner_for(module: str):
    async def _run(row: dict[str, Any]) -> None:
        import importlib

        await importlib.import_module(f"bot.platforms.{module}").run_instance(row)  # runs until cancelled

    return _run


_RUNNERS = {
    "discord": _run_discord, "slack": _run_slack, "telegram": _run_telegram,
    "matrix": _run_matrix, "whatsapp": _run_whatsapp,
    "email": _runner_for("email_platform"), "sms": _runner_for("sms_platform"),
    "signal": _runner_for("signal_platform"), "imessage": _runner_for("imessage_platform"),
    "googlechat": _runner_for("googlechat_platform"), "teams": _runner_for("teams_platform"),
}


# --------------------------------------------- telegram: one poller, one bot --
# The helpers behind _run_telegram: waiting on a 409 instead of a cancel, the
# PTB error callback that reports one, and the status the dashboard reads.

async def _wait_or_conflict(instance_id: int) -> None:
    """Block until this instance is asked to stop (its task is cancelled) or a
    409 Conflict is reported for it."""
    await _conflict_events.setdefault(instance_id, asyncio.Event()).wait()


async def _sleep_or_conflict(instance_id: int, seconds: float) -> None:
    """Sleep, but wake early on a 409 (so a parked instance that is somehow
    being polled anyway still reacts immediately)."""
    event = _conflict_events.setdefault(instance_id, asyncio.Event())
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        pass


def _stop_polling_task(application: Any) -> None:
    """Cancel PTB's own polling task directly.

    `Updater.stop()` is the only public path, but it *awaits* the polling task,
    which after a 409 is sleeping out its error backoff (1 s, 1.5 s, … up to
    30 s) before it re-checks `self.running`. That would hold an instance's
    shutdown open for up to half a minute for no benefit. PTB keeps the task in
    a name-mangled private slot, so read it defensively: a future PTB that
    renames it just means we fall back to the slow-but-correct public stop."""
    task = getattr(getattr(application, "updater", None), "_Updater__polling_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()


def _telegram_error_callback(row: dict[str, Any]):
    """The polling error callback bot/main.py hands to PTB.

    PTB's default (`_LOGGER.exception(...)` + retry forever, backing off to 30 s)
    is the wrong behaviour for exactly one error: `telegram.error.Conflict`,
    which means another program on this machine is polling the same token. That
    is not transient, and retrying is worse than useless — the two pollers knock
    each other off indefinitely. So: stop this instance, record the reason, and
    let nothing restart it (see _handle_conflict)."""
    from telegram.error import Conflict

    def _cb(exc: BaseException) -> None:
        if isinstance(exc, Conflict):
            _handle_conflict(row, exc)

    return _cb


def _handle_conflict(row: dict[str, Any], exc: BaseException) -> None:
    from bot import hermes_gateway
    from bot.diagnostics import telemetry

    instance_id = row["id"]
    message = hermes_gateway.CONFLICT_ERROR
    logger.error(
        "Telegram instance %r (id=%s) got a 409 Conflict - %s (stopping it; it will not "
        "restart on its own, because a second poller of the same token never resolves itself)",
        row["name"], instance_id, message,
    )
    bot_instances.record_error(instance_id, message)
    handle = _handles.get(instance_id)
    if handle is not None:
        handle.error = message
        handle.status = message
    # No auto-restart: _restart_after_crash is the wrong reflex for this error,
    # so drop the backoff state; only an explicit start/restart revives it. The
    # runner itself unwinds when the event below is set (it is the task _done_callback
    # then removes from the live set), so nothing has to be cancelled here.
    _restart_state.pop(instance_id, None)
    event = _conflict_events.get(instance_id)
    if event is not None:
        event.set()
    telemetry.increment("platform.conflict")
    telemetry.record_event("platform_conflict", f"{row['name']} (id={instance_id}): 409 Conflict")


def _set_status(instance_id: int, text: str, *, served_by: str = "") -> None:
    """Publish what this instance's runner is actually doing. `served_by` is set
    only while a running Hermes gateway genuinely owns the token — it is what
    the dashboard shows as "Served by Hermes", and it must go away the moment
    that gateway stops, unlike `status`, which then explains why ABP is still
    not polling."""
    handle = _handles.get(instance_id)
    if handle is not None:
        handle.status = text
        handle.served_by = served_by


def _done_callback(instance_id: int, name: str) -> Any:
    def _cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("bot instance %r (id=%s) ended with an error: %s", name, instance_id, exc, exc_info=exc)
            bot_instances.record_error(instance_id, str(exc))
            from bot.diagnostics import telemetry

            telemetry.increment("platform.crash")
            telemetry.record_event("platform_crash", f"{name} (id={instance_id}): {exc}")
        handle = _handles.get(instance_id)
        if handle is not None and handle.task is task:
            if exc is not None:
                handle.error = str(exc)
                handle.status = str(exc)
            _handles.pop(instance_id, None)
        if exc is not None:
            bg.spawn(_restart_after_crash(instance_id, name))

    return _cb


async def _restart_after_crash(instance_id: int, name: str) -> None:
    """Bring a crashed instance back up on its own, backing off if it
    keeps crashing right away (a real config/code problem) instead of
    hot-looping restarts, but recovering fast from a one-off transient
    failure."""
    now = time.monotonic()
    state = _restart_state.setdefault(instance_id, {"attempt": 0, "last_crash": 0.0})
    if now - state["last_crash"] > _RESTART_RESET_AFTER_S:
        state["attempt"] = 0
    state["attempt"] += 1
    state["last_crash"] = now
    delay = min(2 ** state["attempt"], _RESTART_MAX_BACKOFF_S)
    logger.warning(
        "bot instance %r (id=%s) crashed — restarting automatically in %.0fs (attempt %d)",
        name, instance_id, delay, state["attempt"],
    )
    await asyncio.sleep(delay)
    row = bot_instances.get_instance(instance_id)
    if row is None or not row["enabled"] or instance_id in _handles:
        return  # deleted, disabled, or already restarted (e.g. a manual restart) meanwhile
    try:
        await start_instance(row)
    except Exception as exc:
        logger.error("failed to auto-restart bot instance %r (id=%s) after crash: %s", name, instance_id, exc)
    else:
        from bot.diagnostics import telemetry

        telemetry.increment("platform.auto_restart")


async def start_instance(row: dict[str, Any]) -> None:
    instance_id = row["id"]
    if instance_id in _handles:
        return  # already running
    if row["platform"] == "app":
        return  # no external platform, so no live connection to start - see bot_instances.PLATFORMS
    runner = _RUNNERS.get(row["platform"])
    if runner is None:
        raise ValueError(f"unknown platform {row['platform']!r}")
    task = asyncio.create_task(runner(row))
    task.add_done_callback(_done_callback(instance_id, row["name"]))
    _handles[instance_id] = _Handle(instance_id=instance_id, name=row["name"], platform=row["platform"], task=task)
    bot_instances.record_start(instance_id)
    logger.info("started bot instance %r (id=%s, platform=%s)", row["name"], instance_id, row["platform"])


async def stop_instance(instance_id: int) -> None:
    _restart_state.pop(instance_id, None)
    handle = _handles.pop(instance_id, None)
    if handle is None:
        return
    handle.task.cancel()
    try:
        await handle.task
    except (asyncio.CancelledError, Exception):
        pass
    _conflict_events.pop(instance_id, None)


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
    _conflict_events.clear()


def status() -> dict[int, dict[str, Any]]:
    """Live state per instance. `status`/`served_by` are why a Telegram instance
    that is deliberately not polling (its token belongs to the user's Hermes
    gateway) reads as an accounted-for state instead of a broken one."""
    return {
        instance_id: {
            "running": not handle.task.done(),
            "platform": handle.platform,
            "name": handle.name,
            "status": handle.status,
            "served_by": handle.served_by,
            "error": handle.error,
        }
        for instance_id, handle in _handles.items()
    }


def is_running(instance_id: int) -> bool:
    handle = _handles.get(instance_id)
    return handle is not None and not handle.task.done()


def served_by(instance_id: int) -> str:
    """The Hermes gateway currently serving this instance's Telegram token, or
    "" when AgenticBotPlatform owns it. Safe to call for a never-started
    instance: it asks bot/hermes_gateway.py directly rather than only reporting
    what a running task already decided."""
    handle = _handles.get(instance_id)
    if handle is not None and handle.served_by:
        return handle.served_by
    row = bot_instances.get_instance(instance_id)
    if row is None:
        return ""
    from bot import hermes_gateway

    return hermes_gateway.instance_status(row)
