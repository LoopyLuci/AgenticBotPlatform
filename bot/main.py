"""Entrypoint — runs the Telegram bot and the dashboard API in one process,
sharing one asyncio event loop and one SQLite connection.

Usage:
    python -m bot.main
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal

from dotenv import load_dotenv

from bot.envfile import PROJECT_ROOT as ROOT
from bot.envfile import resolve as resolve_env_path

_env_path = resolve_env_path()
load_dotenv(_env_path)

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


logger = logging.getLogger("bot.main")


def _handle_asyncio_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows' Proactor event loop routinely raises ConnectionResetError
    from its own _call_connection_lost cleanup when a long-poll socket
    (Telegram's HTTP client cycling connections) gets closed by the remote
    side first — harmless, but the default handler logs it at ERROR with a
    full traceback on every occurrence, which under load can happen often
    enough to drown out errors that actually matter. Everything else still
    goes through asyncio's normal default handling unchanged."""
    exc = context.get("exception")
    handle = context.get("handle")
    if (
        isinstance(exc, ConnectionResetError)
        and handle is not None
        and "_call_connection_lost" in repr(handle)
    ):
        logger.debug("benign Proactor connection-lost cleanup: %s", exc)
        return
    loop.default_exception_handler(context)


async def build_telegram_instance(row: dict) -> "telegram.ext.Application":
    """Builds, initializes, and starts polling for one Telegram bot
    instance — called once per enabled bot_instances row with
    platform="telegram" (bot/platform_supervisor.py owns the task that
    keeps each one alive). Public (not prefixed with _) since
    platform_supervisor imports it directly.
    """
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

    from bot import handlers, outbox, slash_commands

    token = row["credentials"]["bot_token"]
    application = Application.builder().token(token).build()
    application.bot_data["instance_id"] = row["id"]
    application.bot_data["instance_name"] = row["name"]
    application.bot_data["allowed_ids"] = {int(i) for i in row["allowed_user_ids"]}

    outbox.register(row["id"], lambda chat_id, text: application.bot.send_message(chat_id=chat_id, text=text))
    outbox.register_threaded(
        row["id"],
        lambda chat_id, text, thread_id: application.bot.send_message(
            chat_id=chat_id, text=text, message_thread_id=int(thread_id)
        ),
    )

    async def _send_file(chat_id, file_path, filename, caption):
        with open(file_path, "rb") as f:
            await application.bot.send_document(chat_id=chat_id, document=f, filename=filename, caption=caption)

    outbox.register_file_sender(row["id"], _send_file)

    # Table-driven registration: every command lives once in
    # bot/slash_commands.py's registry (name + every alias), and one
    # CommandHandler dispatches all of them through handlers.on_command,
    # which resolves aliases and picks the right implementation — see that
    # module's docstring for why (mirrors the real Hermes Agent's
    # single-entry-point command dispatch instead of one PTB handler per
    # command, which had let /new_session silently go unregistered here).
    application.add_handler(CommandHandler(slash_commands.all_dispatchable_names(), handlers.on_command))
    application.add_handler(CallbackQueryHandler(handlers.on_callback))
    application.add_handler(MessageHandler(filters.Document.ALL, handlers.on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))

    await application.initialize()

    # Populates Telegram's native "/" command menu — nothing did this
    # before, so the blue "/" button showed nothing. Registered across all
    # three scopes so it shows up the same in DMs and groups.
    menu_commands = [BotCommand(name, desc) for name, desc in slash_commands.telegram_menu_commands()]
    for scope_cls in (BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats):
        try:
            await application.bot.set_my_commands(menu_commands, scope=scope_cls())
        except Exception:
            logger.exception(
                "failed to register Telegram command menu (scope=%s) for instance %r",
                scope_cls.__name__, row["name"],
            )

    await application.start()
    await application.updater.start_polling()
    logger.info("Telegram bot instance %r (id=%s) connected", row["name"], row["id"])
    return application


async def run() -> None:
    from bot import bot_instances, db, platform_supervisor
    from bot.config import config

    db.init_db()
    logger.info("secrets loaded from %s (exists=%s)", _env_path, _env_path.exists())
    db.log_audit(actor="system", action="startup", detail=f"env: {_env_path}")

    migrated_id = bot_instances.migrate_legacy_env_instance()
    if migrated_id is not None:
        logger.info("migrated legacy .env Telegram config into bot instance #%s", migrated_id)

    instances = bot_instances.list_instances(enabled_only=True)
    await platform_supervisor.start_all_enabled(instances)

    if not instances:
        raise SystemExit(
            "No bot is configured — add one from the dashboard's Bots tab "
            "(or scripts/setup.py) and restart."
        )

    # dashboard app, sharing this process/loop
    import uvicorn

    from bot.dashboard.server import build_app

    dash_app = build_app()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    uv_config = uvicorn.Config(dash_app, host=host, port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(uv_config)

    stop_event = asyncio.Event()

    def _handle_signal(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_handle_asyncio_exception)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for SIGTERM

    dashboard_task = asyncio.create_task(server.serve())
    watch_task = asyncio.create_task(config.watch_forever())

    from bot import scheduler

    scheduler_task = asyncio.create_task(scheduler.run_forever(stop_event))

    # server.serve() just got scheduled, not confirmed running — uvicorn
    # sets Server.started once it's actually bound and accepting
    # connections. Wait for that (bounded, so a real bind failure still
    # surfaces promptly) before claiming "listening": logging this before
    # it's true is exactly the kind of thing that makes a startup failure
    # look like a working server in the log.
    for _ in range(100):  # 100 x 0.05s = 5s
        if server.started or dashboard_task.done():
            break
        await asyncio.sleep(0.05)
    if dashboard_task.done():
        dashboard_task.result()  # re-raise the real bind error, if any
    logger.info("Dashboard listening on http://%s:%s", host, port)

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        watch_task.cancel()
        await scheduler_task  # stop_event is already set; run_forever exits its own loop cleanly
        server.should_exit = True
        await dashboard_task
        await platform_supervisor.stop_all()
        from bot.router import router as _router

        await _router.shutdown_backends()
        db.log_audit(actor="system", action="shutdown")


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
