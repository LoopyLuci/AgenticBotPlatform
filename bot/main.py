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


def _log_task_exception(name: str, task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("%s platform task ended with an error: %s", name, exc, exc_info=exc)


async def _build_telegram(outbox) -> "telegram.ext.Application | None":
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

    from bot import handlers

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram platform skipped")
        return None

    application = Application.builder().token(token).build()
    outbox.register("telegram", lambda chat_id, text: application.bot.send_message(chat_id=chat_id, text=text))

    application.add_handler(CommandHandler("start", handlers.cmd_start))
    application.add_handler(CommandHandler("help", handlers.cmd_start))
    application.add_handler(CommandHandler("ask", handlers.cmd_ask))
    application.add_handler(CommandHandler("status", handlers.cmd_status))
    application.add_handler(CommandHandler("start_desktop", handlers.cmd_start_desktop))
    application.add_handler(CommandHandler("stop_desktop", handlers.cmd_stop_desktop))
    application.add_handler(CommandHandler("restart_desktop", handlers.cmd_restart_desktop))
    application.add_handler(CommandHandler("backend", handlers.cmd_backend))
    application.add_handler(CommandHandler("mcp", handlers.cmd_mcp))
    application.add_handler(CommandHandler("project", handlers.cmd_project))
    application.add_handler(CallbackQueryHandler(handlers.on_callback))
    application.add_handler(MessageHandler(filters.Document.ALL, handlers.on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("Telegram platform connected")
    return application


async def run() -> None:
    from bot import db, desktop, outbox
    from bot.config import config
    from bot.platforms import discord_platform, slack_platform

    db.init_db()
    logger.info("secrets loaded from %s (exists=%s)", _env_path, _env_path.exists())
    db.log_audit(actor="system", action="startup", detail=f"env: {_env_path}")

    telegram_app = await _build_telegram(outbox)

    platform_tasks: list[tuple[str, asyncio.Task]] = []
    if discord_platform.is_configured():
        t = asyncio.create_task(discord_platform.start())
        t.add_done_callback(lambda tk: _log_task_exception("discord", tk))
        platform_tasks.append(("discord", t))
    elif os.environ.get("DISCORD_BOT_TOKEN"):
        logger.warning("DISCORD_BOT_TOKEN is set but DISCORD_ALLOWED_USER_IDS is missing — Discord platform skipped")

    if slack_platform.is_configured():
        t = asyncio.create_task(slack_platform.start())
        t.add_done_callback(lambda tk: _log_task_exception("slack", tk))
        platform_tasks.append(("slack", t))
    elif os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_APP_TOKEN"):
        logger.warning("Slack tokens are only partially set (need SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and SLACK_ALLOWED_USER_IDS) — Slack platform skipped")

    if telegram_app is None and not platform_tasks:
        raise SystemExit(
            "No messaging platform is configured — set up Telegram, Discord, or Slack "
            "from the dashboard's Platforms settings (or scripts/setup.py) and restart."
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for SIGTERM

    dashboard_task = asyncio.create_task(server.serve())
    watch_task = asyncio.create_task(config.watch_forever())
    logger.info("Dashboard listening on http://%s:%s", host, port)

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        watch_task.cancel()
        server.should_exit = True
        await dashboard_task
        for name, task in platform_tasks:
            task.cancel()
        if discord_platform.is_configured():
            await discord_platform.stop()
        if slack_platform.is_configured():
            await slack_platform.stop()
        if telegram_app is not None:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        db.log_audit(actor="system", action="shutdown")


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
