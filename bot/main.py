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
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

    from bot import handlers, outbox

    token = row["credentials"]["bot_token"]
    application = Application.builder().token(token).build()
    application.bot_data["instance_id"] = row["id"]
    application.bot_data["instance_name"] = row["name"]
    application.bot_data["allowed_ids"] = {int(i) for i in row["allowed_user_ids"]}

    outbox.register(row["id"], lambda chat_id, text: application.bot.send_message(chat_id=chat_id, text=text))

    async def _send_file(chat_id, file_path, filename, caption):
        with open(file_path, "rb") as f:
            await application.bot.send_document(chat_id=chat_id, document=f, filename=filename, caption=caption)

    outbox.register_file_sender(row["id"], _send_file)

    application.add_handler(CommandHandler("start", handlers.cmd_start))
    application.add_handler(CommandHandler("help", handlers.cmd_start))
    application.add_handler(CommandHandler("ask", handlers.cmd_ask))
    application.add_handler(CommandHandler("status", handlers.cmd_status))
    application.add_handler(CommandHandler("start_desktop", handlers.cmd_start_desktop))
    application.add_handler(CommandHandler("stop_desktop", handlers.cmd_stop_desktop))
    application.add_handler(CommandHandler("restart_desktop", handlers.cmd_restart_desktop))
    application.add_handler(CommandHandler("backend", handlers.cmd_backend))
    application.add_handler(CommandHandler("model", handlers.cmd_model))
    application.add_handler(CommandHandler("mcp", handlers.cmd_mcp))
    application.add_handler(CommandHandler("project", handlers.cmd_project))
    application.add_handler(CallbackQueryHandler(handlers.on_callback))
    application.add_handler(MessageHandler(filters.Document.ALL, handlers.on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))

    await application.initialize()
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
