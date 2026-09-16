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
import sys
import threading

from dotenv import load_dotenv

from bot.envfile import PROJECT_ROOT as ROOT
from bot.envfile import ensure_dashboard_token, resolve as resolve_env_path

_env_path = resolve_env_path()
load_dotenv(_env_path)
# A fresh install's .env has no DASHBOARD_TOKEN yet — generate and persist
# one now rather than ever asking a human to invent/paste one. Must run
# after load_dotenv() (so an existing token already in the process's
# real env wins) but before anything reads DASHBOARD_TOKEN.
os.environ.setdefault("DASHBOARD_TOKEN", ensure_dashboard_token())

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    """sys.excepthook replacement — catches anything that escapes all the
    way to the top of the main thread uncaught. Every OTHER error path in
    this file already flows through logging (a normal try/except that
    calls logger.exception/.error, or _handle_asyncio_exception below for
    a task's own unhandled exception) and therefore already reaches both
    logs/bot.log and the Activity tab (bot/activity_log.py's ring buffer
    sits on the root logger) — this is specifically the one class of
    error that wouldn't: a genuinely unhandled exception that unwinds the
    whole process. Without this, that exact class of failure printed to
    stderr only (Python's default behavior) and left zero trace in either
    place, exactly the kind of silent, hard-to-diagnose crash this
    project has hit more than once. Still calls the real default hook
    afterward, so a direct terminal run keeps seeing the traceback too."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logging.getLogger("bot.uncaught").critical(
        "unhandled exception reached the top level — the process is about to exit",
        exc_info=(exc_type, exc_value, exc_tb),
    )
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    """threading.excepthook replacement — sys.excepthook above only ever
    fires for the main thread; this is the same safety net for any other
    (a background worker, an asyncio.to_thread call, a library's own
    thread) that lets an exception escape uncaught."""
    thread_name = args.thread.name if args.thread is not None else "?"
    logging.getLogger("bot.uncaught").critical(
        "unhandled exception in thread %r", thread_name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    threading.__excepthook__(args)


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

    sys.excepthook = _log_uncaught_exception
    threading.excepthook = _log_uncaught_thread_exception

    from bot import activity_log, diagnostics

    activity_log.install()
    diagnostics.install()


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
    application.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))
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


async def _start_dashboard(dash_app, host: str, port: int, *, max_attempts: int = 8, retry_delay_s: float = 2.0):
    """Binds and starts the dashboard's uvicorn server, retrying on a bind
    failure instead of letting it take the whole process down.

    uvicorn's own Server.startup() calls `sys.exit(uvicorn.config.
    STARTUP_FAILURE)` — literally `3` — directly on an OSError from
    binding the socket (confirmed by reading uvicorn/server.py and
    uvicorn/config.py). That SystemExit propagates straight out of
    `asyncio.run(run())` and kills the entire interpreter, Telegram/
    Discord/etc. bots included, over what is very often a transient,
    self-resolving port conflict — most commonly a previous instance of
    this exact process that hasn't fully released the socket yet (the
    desktop app's own Rust side now also detects and handles this before
    ever spawning bot.main, but this process can just as well be started
    directly, by an older desktop build, or with something else briefly
    holding the port for any other reason — this needs to be safe on its
    own, not only when something else's pre-check has already run).

    A dead uvicorn.Server can't be restarted, so this builds a brand new
    Config/Server/task each attempt. Returns (server, dashboard_task)
    once genuinely bound and accepting connections, or (None, None) if
    every attempt failed — the caller keeps every OTHER subsystem
    (Telegram/Discord bots, scheduler, retention, mDNS, hot-reload)
    running either way, rather than taking the whole process down over
    the dashboard API alone.

    Critical detail that broke the first version of this fix: a Task
    whose coroutine raises SystemExit/KeyboardInterrupt is NOT handled
    like a Task raising a normal Exception — asyncio's own Task-stepping
    machinery deliberately lets those two propagate straight out of the
    event loop instead of storing them for a later `.result()`/
    `.exception()` call, confirmed by reproducing this fix's exact
    failure live (the retry loop below never even ran a second attempt;
    the whole process still died on the very first one). Catching
    SystemExit has to happen INSIDE the same coroutine frame uvicorn
    raises it from — _serve() below — never after the fact via the
    task's own result."""
    import uvicorn

    async def _serve(server: "uvicorn.Server") -> None:
        try:
            await server.serve()
        except SystemExit:
            # uvicorn's own bind-failure signal (see this function's
            # docstring) — swallowed here, at the source, so it can never
            # reach asyncio's Task-stepping machinery as an uncaught
            # BaseException and take the whole event loop down with it.
            pass

    for attempt in range(1, max_attempts + 1):
        uv_config = uvicorn.Config(dash_app, host=host, port=port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(uv_config)
        dashboard_task = asyncio.create_task(_serve(server))
        for _ in range(100):  # 100 x 0.05s = 5s
            if server.started or dashboard_task.done():
                break
            await asyncio.sleep(0.05)
        if server.started:
            if attempt > 1:
                logger.info("dashboard API bound %s:%s on attempt %s/%s", host, port, attempt, max_attempts)
            return server, dashboard_task
        # Bind failed — uvicorn's own logger.error(exc) already printed
        # the real OSError above this. Make sure the task is actually
        # finished (it should be, _serve() already swallowed the
        # SystemExit) before starting a fresh attempt.
        if not dashboard_task.done():
            dashboard_task.cancel()
        try:
            await dashboard_task
        except asyncio.CancelledError:
            pass
        logger.warning(
            "dashboard API failed to bind %s:%s (attempt %s/%s)",
            host, port, attempt, max_attempts,
        )
        if attempt < max_attempts:
            await asyncio.sleep(retry_delay_s)
    logger.critical(
        "dashboard API could not bind %s:%s after %s attempts — continuing WITHOUT it. "
        "Telegram/Discord/other configured platforms are still running normally. Close "
        "whatever else is using this port (check `netstat -ano | findstr :%s` on Windows) "
        "and restart AgenticBotPlatform to restore the dashboard/GUI.",
        host, port, max_attempts, port,
    )
    return None, None


async def run() -> None:
    from bot import bot_instances, db, platform_supervisor
    from bot.config import config

    db.init_db()
    logger.info("secrets loaded from %s (exists=%s)", _env_path, _env_path.exists())
    db.log_audit(actor="system", action="startup", detail=f"env: {_env_path}")

    from bot import plugins as plugin_registry

    plugin_registry.load_enabled()

    from bot.agent_runtime import mcp_client

    await mcp_client.connect_all_enabled()

    migrated_id = bot_instances.migrate_legacy_env_instance()
    if migrated_id is not None:
        logger.info("migrated legacy .env Telegram config into bot instance #%s", migrated_id)

    instances = bot_instances.list_instances(enabled_only=True)
    await platform_supervisor.start_all_enabled(instances)

    if not instances:
        logger.info(
            "no bot instances configured yet — dashboard/desktop UI is still "
            "available to add one from the Bots tab"
        )

    # dashboard app, sharing this process/loop — see _start_dashboard()
    # above for the actual uvicorn.Server construction/retry.
    from bot.dashboard.server import build_app

    dash_app = build_app()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))

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

    watch_task = asyncio.create_task(config.watch_forever())

    from bot import hotreload

    hotreload_task = asyncio.create_task(hotreload.watch_forever())

    from bot import scheduler

    scheduler_task = asyncio.create_task(scheduler.run_forever(stop_event))

    # Reactive half of auto-management (bot/auto_manage.py) — a new
    # kanban card fires a real check-in for that board's owning instance,
    # if it's configured to react to this trigger. The scheduled half
    # needs no separate wiring here: it's a normal scheduled_commands row
    # (kind="auto_manage") the scheduler task above already polls.
    from bot import auto_manage, db as _db

    def _on_kanban_card_created(card_id: int) -> None:
        asyncio.create_task(auto_manage.maybe_trigger_from_kanban_card(card_id))

    _db.on_kanban_card_created(_on_kanban_card_created)

    from bot import peers

    peers_health_task = asyncio.create_task(peers.health_check_forever(stop_event))

    from bot import retention

    retention_task = asyncio.create_task(retention.run_forever(stop_event))

    from bot import mdns_advertise

    # Blocking (real socket I/O to send the mDNS announcement) but brief and
    # one-shot — off the event loop rather than a long-lived task. Failure
    # here (no multicast-capable network stack, etc.) is logged and
    # swallowed inside start() itself; this is discovery sugar for the
    # Android app's NsdDiscoveryClient, never a startup dependency.
    await asyncio.to_thread(mdns_advertise.start, port)

    # Holds whatever _start_dashboard() last returned, read by the
    # shutdown path below — a plain dict since dashboard_supervisor()
    # reassigns it from inside a background task, and shutdown needs to
    # see whatever the CURRENT values are at that point, not whatever
    # they were at the moment this task was created.
    dashboard_state: dict[str, object] = {"server": None, "task": None}

    async def dashboard_supervisor() -> None:
        """_start_dashboard()'s own max_attempts is a bounded initial
        burst (fast retries for the common case: something releases the
        port within a few seconds). If that's genuinely not enough —
        whatever's holding the port sticks around much longer — keep
        trying indefinitely in the background at a much slower interval
        instead of requiring the user to notice and manually restart the
        whole app once it clears on its own. Exits as soon as either a
        bind succeeds or the process is shutting down."""
        server, task = await _start_dashboard(dash_app, host, port)
        while server is None and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
                break  # stop_event fired while waiting — shutting down, stop trying
            except asyncio.TimeoutError:
                pass
            if stop_event.is_set():
                break
            server, task = await _start_dashboard(dash_app, host, port, max_attempts=1)
        dashboard_state["server"] = server
        dashboard_state["task"] = task
        if server is not None:
            logger.info("Dashboard listening on http://%s:%s", host, port)

    dashboard_supervisor_task = asyncio.create_task(dashboard_supervisor())

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        watch_task.cancel()
        hotreload_task.cancel()
        await scheduler_task  # stop_event is already set; run_forever exits its own loop cleanly
        await peers_health_task  # same shutdown contract as scheduler_task
        await retention_task  # same shutdown contract as scheduler_task
        await asyncio.to_thread(mdns_advertise.stop)
        await dashboard_supervisor_task  # let its own retry loop notice stop_event and exit
        server = dashboard_state["server"]
        dashboard_task = dashboard_state["task"]
        if server is not None:  # None if every bind attempt ever failed
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
