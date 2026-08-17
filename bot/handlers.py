"""Telegram command handlers.

Every handler is wrapped with @require_auth — anything from a user not on
the allowlist is dropped silently (from their point of view) and logged.
"""

from __future__ import annotations

import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import auth, db, desktop, setup_wizard
from bot.backends.base import BackendError
from bot.config import config
from bot.envfile import PROJECT_ROOT
from bot.router import VALID_BACKENDS, router

logger = logging.getLogger("bot.handlers")

TELEGRAM_MAX_LEN = 4096
INBOX_DIR = PROJECT_ROOT / "data" / "inbox"


def require_auth(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not auth.is_allowed(user.id):
            if user:
                auth.reject_and_log(user.id, user.username or "")
            return
        # Single choke point for every authorized incoming update (commands
        # and plain text alike) — the dashboard's chat view reads this table
        # directly, so it mirrors exactly what's happening on Telegram
        # without each handler needing to remember to log itself.
        msg = update.message
        text = (msg.text or msg.caption) if msg else None
        if text:
            db.log_message(
                chat_id=update.effective_chat.id,
                user_id=user.id,
                username=user.username or "",
                direction="in",
                source="telegram",
                text=text,
            )
        return await handler(update, context)

    return wrapped


async def _reply_chunked(update: Update, text: str) -> None:
    text = text or "(empty response)"
    db.log_message(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id if update.effective_user else None,
        direction="out",
        source="bot",
        text=text,
    )
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        await update.message.reply_text(text[i : i + TELEGRAM_MAX_LEN])


def _parse_backend_flag(text: str) -> tuple[str, str | None]:
    """Strip a trailing --backend=x flag off a message, if present."""
    parts = text.rsplit("--backend=", 1)
    if len(parts) == 2:
        rest, override = parts
        override = override.strip().split()[0]
        if override in VALID_BACKENDS:
            return rest.strip(), override
    return text, None


# --------------------------------------------------------------- basic ----

@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_chunked(
        update,
        "Bot Control is online.\n\n"
        "/ask <text> — send a prompt (append --backend=api|cli|ui to override)\n"
        "/status — health snapshot\n"
        "/backend show|set — router config\n"
        "/mcp list|enable|disable|logs — MCP servers\n"
        "/start_desktop /stop_desktop /restart_desktop\n"
        "/project open <path> — set working dir for the next /ask",
    )


@require_auth
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args) if context.args else ""
    await _handle_ask(update, context, raw)


@require_auth
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_ask(update, context, update.message.text or "")


async def _handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    prompt, backend_override = _parse_backend_flag(raw)
    if not prompt:
        await _reply_chunked(update, "Usage: /ask <text> [--backend=api|cli|ui]")
        return

    action_type = context.user_data.get("action_type", "quick_question")
    cwd = context.user_data.get("project_cwd")

    await update.message.chat.send_action("typing")
    try:
        result = await router.ask(
            prompt,
            action_type=action_type,
            user_id=update.effective_user.id,
            backend_override=backend_override,
            context={"cwd": cwd} if cwd else None,
        )
        await _reply_chunked(update, result.text)
    except BackendError as exc:
        await _reply_chunked(update, f"Backend failed: {exc}")


# --------------------------------------------------------------- status ---

@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ov = db.get_overview()
    d = desktop.status()
    cfg = config.current
    readiness = setup_wizard.backend_readiness()
    backend_lines = []
    for name in ("api", "cli", "ui"):
        info = readiness[name]
        mark = "ready" if info["ready"] else f"not set up ({info['reason']})"
        used = " · in use" if info["in_use"] else ""
        backend_lines.append(f"  {name}: {mark}{used}")
    lines = [
        f"Desktop: {'running' if d.get('running') else 'stopped'}" + (f" (pid {d['pid']})" if d.get("pid") else ""),
        f"Default backend: {cfg.get('default_backend')}",
        "Backends:",
        *backend_lines,
        f"Jobs running: {ov['jobs_running']} · queued: {ov['jobs_queued']}",
        f"Completed today: {ov['completed_today']} · failed: {ov['failed_today']}",
        f"Success rate (7d): {ov['success_rate_7d']}%",
        f"Avg duration: {ov['avg_duration_ms']}ms",
        f"Config version: v{config.version}",
    ]
    await _reply_chunked(update, "\n".join(lines))


# ---------------------------------------------------------- desktop ctrl --

def _confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm", callback_data=f"confirm:{action}"),
          InlineKeyboardButton("Cancel", callback_data="cancel")]]
    )


@require_auth
async def cmd_start_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        desktop.start()
        await _reply_chunked(update, "Claude Desktop starting.")
    except FileNotFoundError as exc:
        await _reply_chunked(update, str(exc))


@require_auth
async def cmd_stop_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (config.current.get("security") or {}).get("confirm_destructive", True):
        await update.message.reply_text("Stop Claude Desktop?", reply_markup=_confirm_keyboard("stop_desktop"))
    else:
        desktop.stop()
        await _reply_chunked(update, "Claude Desktop stopped.")


@require_auth
async def cmd_restart_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (config.current.get("security") or {}).get("confirm_destructive", True):
        await update.message.reply_text("Restart Claude Desktop?", reply_markup=_confirm_keyboard("restart_desktop"))
    else:
        desktop.restart()
        await _reply_chunked(update, "Claude Desktop restarted.")


@require_auth
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return
    if not query.data.startswith("confirm:"):
        return
    action = query.data.split(":", 1)[1]
    if action == "stop_desktop":
        desktop.stop()
        await query.edit_message_text("Claude Desktop stopped.")
    elif action == "restart_desktop":
        desktop.restart()
        await query.edit_message_text("Claude Desktop restarted.")
    elif action.startswith("mcp_disable:"):
        name = action.split(":", 1)[1]
        desktop.disable_mcp(name)
        await query.edit_message_text(f"MCP server {name!r} disabled. Restart Desktop to apply.")


# ------------------------------------------------------------- backend ----

@require_auth
async def cmd_backend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    cfg = config.current
    if not args or args[0] == "show":
        overrides = cfg.get("action_overrides", {})
        lines = [f"default: {cfg.get('default_backend')}"]
        for action, entry in overrides.items():
            lines.append(f"{action}: {entry.get('backend')} (backup: {entry.get('backup', [])})")
        await _reply_chunked(update, "\n".join(lines))
        return
    if args[0] == "set" and len(args) >= 3:
        action_or_default, name = args[1], args[2]
        if name not in VALID_BACKENDS:
            await _reply_chunked(update, f"unknown backend {name!r}")
            return
        if action_or_default == "default":
            config.set_value(["default_backend"], name, actor=str(update.effective_user.id))
        else:
            config.set_value(["action_overrides", action_or_default, "backend"], name, actor=str(update.effective_user.id))
        await _reply_chunked(update, f"Set {action_or_default} -> {name} (v{config.version})")
        return
    await _reply_chunked(update, "Usage: /backend show | /backend set <action|default> <api|cli|ui>")


# ----------------------------------------------------------------- mcp ----

@require_auth
async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or args[0] == "list":
        servers = desktop.list_mcp_servers()
        if not servers:
            await _reply_chunked(update, "No MCP servers configured.")
            return
        lines = [f"{'✓' if s['enabled'] else '✗'} {s['name']}" for s in servers]
        await _reply_chunked(update, "\n".join(lines))
        return
    if args[0] in ("enable", "disable") and len(args) >= 2:
        name = args[1]
        try:
            if args[0] == "enable":
                desktop.enable_mcp(name)
            else:
                desktop.disable_mcp(name)
            await _reply_chunked(update, f"{name} {args[0]}d. Restart Desktop to apply.")
        except KeyError as exc:
            await _reply_chunked(update, str(exc))
        return
    if args[0] == "logs" and len(args) >= 2:
        lines = desktop.tail_mcp_log(args[1], lines=30)
        await _reply_chunked(update, "\n".join(lines))
        return
    await _reply_chunked(update, "Usage: /mcp list | enable <name> | disable <name> | logs <name>")


# ------------------------------------------------------------- project ----

@require_auth
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) >= 2 and args[0] == "open":
        path = " ".join(args[1:])
        context.user_data["project_cwd"] = path
        context.user_data["action_type"] = "project_task"
        await _reply_chunked(update, f"Project set to {path} — next /ask runs with action_type=project_task")
        return
    await _reply_chunked(update, "Usage: /project open <path>")


# ---------------------------------------------------------------- files ---

@require_auth
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = INBOX_DIR / doc.file_name
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(custom_path=str(dest))
    db.log_audit(actor=str(update.effective_user.id), action="file_upload", detail=str(dest))
    await _reply_chunked(update, f"Saved to inbox: {dest.name}. Reference it in your next /ask.")
