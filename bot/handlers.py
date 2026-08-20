"""Telegram command handlers.

Every handler is wrapped with @require_auth — anything from a user not on
this Telegram bot instance's allowlist is dropped silently (from their
point of view) and logged.

One Application (see bot/main.py's _build_telegram_instance) is built per
enabled Telegram bot_instances row, and each carries its own instance_id
and allowed_ids in application.bot_data — a per-Application dict
python-telegram-bot provides for exactly this (shared state visible to
every handler that Application runs, isolated from any other Application
instance's bot_data). Handlers read `context.bot_data["instance_id"]` /
`context.bot_data["allowed_ids"]` instead of a single global allowlist, so
the same handler functions work correctly for any number of concurrently
running Telegram bots.
"""

from __future__ import annotations

import asyncio
import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import attachments, commands, db, desktop, push
from bot.commands import CmdContext
from bot.config import config

logger = logging.getLogger("bot.handlers")

TELEGRAM_MAX_LEN = 4096


def require_auth(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        allowed_ids = context.bot_data.get("allowed_ids", set())
        instance_id = context.bot_data.get("instance_id")
        if not user or user.id not in allowed_ids:
            if user:
                logger.warning(
                    "rejected message from unauthorized user %s (%s) on instance %s",
                    user.id, user.username or "", instance_id,
                )
                db.log_audit(
                    actor=str(user.id), action="unauthorized_attempt",
                    detail=f"{user.username or ''} (telegram instance {instance_id})",
                )
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
                instance_id=instance_id,
            )
            asyncio.create_task(push.notify_new_message(context.bot_data.get("instance_name", "Bot"), text))
        return await handler(update, context)

    return wrapped


async def _reply_chunked(update: Update, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = text or "(empty response)"
    db.log_message(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id if update.effective_user else None,
        direction="out",
        source="bot",
        text=text,
        instance_id=context.bot_data.get("instance_id"),
    )
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        await update.message.reply_text(text[i : i + TELEGRAM_MAX_LEN])


def _ctx_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> CmdContext:
    return CmdContext(
        instance_id=context.bot_data.get("instance_id"),
        instance_name=context.bot_data.get("instance_name", "Bot"),
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        actor=str(update.effective_user.id),
        session=context.user_data,
    )


# --------------------------------------------------------------- basic ----

@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_chunked(update, commands.HELP_TEXT, context)


@require_auth
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args) if context.args else ""
    await _handle_ask(update, context, raw)


@require_auth
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_ask(update, context, update.message.text or "")


async def _handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    await update.message.chat.send_action("typing")
    reply = await commands.cmd_ask(_ctx_from(update, context), raw)
    await _reply_chunked(update, reply, context)


# --------------------------------------------------------------- status ---

@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_status(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# -------------------------------------------------------------- models ----
# /model with no args opens the same two-level inline-keyboard picker the
# real Hermes Agent Telegram bot uses (backend list with a checkmark on the
# current one -> paginated model list with Prev/Next/Back/Cancel -> tap to
# set, editing the message in place at every step). Discord/Slack keep the
# plain-text commands.cmd_model form (see bot/commands.py) since neither
# platform integration here builds inline-keyboard widgets.

def _model_root_text() -> str:
    lines = ["Choose a backend to change its model:"]
    for info in commands.model_backend_summary():
        mark = "✓ " if info["is_default_backend"] else "  "
        current = info["current_model"] or "(default)"
        lines.append(f"{mark}{info['name']}: {current}")
    return "\n".join(lines)


def _model_root_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for info in commands.model_backend_summary():
        label = ("✓ " if info["is_default_backend"] else "") + info["name"]
        label += f" ({info['count']})" if info["count"] is not None else " (custom)"
        rows.append([InlineKeyboardButton(label, callback_data=f"modelpick:backend:{info['name']}:0")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")])
    return InlineKeyboardMarkup(rows)


def _model_page(backend: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    data = commands.model_page(backend, page)
    rows = []
    if data["has_known_list"]:
        for m in data["models"]:
            mark = "✓ " if m == data["current_model"] else ""
            rows.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"modelpick:set:{backend}:{m}")])
        nav = []
        if data["page"] > 0:
            nav.append(InlineKeyboardButton("< Prev", callback_data=f"modelpick:backend:{backend}:{data['page'] - 1}"))
        if data["page"] < data["total_pages"] - 1:
            nav.append(InlineKeyboardButton("Next >", callback_data=f"modelpick:backend:{backend}:{data['page'] + 1}"))
        if nav:
            rows.append(nav)
        text = f"{backend} — pick a model (current: {data['current_model'] or '(default)'}):"
    else:
        text = (
            f"{backend} has no known model list — Hermes CLI's own models aren't discoverable "
            f"from here.\nReply with `/model set {backend} <name>` to set one directly.\n"
            f"Current: {data['current_model'] or '(default)'}"
        )
    rows.append([InlineKeyboardButton("< Back", callback_data="modelpick:root"), InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")])
    return text, InlineKeyboardMarkup(rows)


@require_auth
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(_model_root_text(), reply_markup=_model_root_keyboard())
        return
    if len(args) == 1 and args[0] not in ("show", "set"):
        # Hermes-style shorthand: `/model <name>` with no backend — infer it
        # from the default backend, same as Hermes inferring the active
        # provider. Only makes sense if the default backend even has a model
        # setting (cli/ui don't).
        default_backend = config.current.get("default_backend")
        if default_backend not in commands.MODEL_BACKENDS:
            await _reply_chunked(
                update,
                f"Default backend {default_backend!r} has no model setting — use "
                f"/model set <api|hermes_cli|hermes_gateway> <name>, or /model to open the picker.",
                context,
            )
            return
        reply = await commands.cmd_model(_ctx_from(update, context), ["set", default_backend, args[0]])
        await _reply_chunked(update, reply, context)
        return
    reply = await commands.cmd_model(_ctx_from(update, context), args)
    await _reply_chunked(update, reply, context)


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
        await _reply_chunked(update, "Claude Desktop starting.", context)
    except FileNotFoundError as exc:
        await _reply_chunked(update, str(exc), context)


@require_auth
async def cmd_stop_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (config.current.get("security") or {}).get("confirm_destructive", True):
        await update.message.reply_text("Stop Claude Desktop?", reply_markup=_confirm_keyboard("stop_desktop"))
    else:
        desktop.stop()
        await _reply_chunked(update, "Claude Desktop stopped.", context)


@require_auth
async def cmd_restart_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (config.current.get("security") or {}).get("confirm_destructive", True):
        await update.message.reply_text("Restart Claude Desktop?", reply_markup=_confirm_keyboard("restart_desktop"))
    else:
        desktop.restart()
        await _reply_chunked(update, "Claude Desktop restarted.", context)


@require_auth
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data in ("cancel", "modelpick:cancel"):
        await query.edit_message_text("Cancelled.")
        return
    if data == "modelpick:root":
        await query.edit_message_text(_model_root_text(), reply_markup=_model_root_keyboard())
        return
    if data.startswith("modelpick:backend:"):
        _, _, backend, page = data.split(":", 3)
        text, keyboard = _model_page(backend, int(page))
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("modelpick:set:"):
        _, _, backend, model = data.split(":", 3)
        reply = commands.apply_model(backend, model, actor=str(update.effective_user.id))
        await query.edit_message_text(reply)
        return
    if not data.startswith("confirm:"):
        return
    action = data.split(":", 1)[1]
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
    reply = await commands.cmd_backend(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ----------------------------------------------------------------- mcp ----

@require_auth
async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_mcp(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ------------------------------------------------------------- project ----

@require_auth
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_project(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ---------------------------------------------------------------- files ---

@require_auth
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    data = bytes(await file.download_as_bytearray())
    rel_path, orig_name = attachments.safe_store(doc.file_name, data)
    db.log_audit(actor=str(update.effective_user.id), action="file_upload", detail=rel_path)
    db.log_message(
        chat_id=update.effective_chat.id, direction="in", source="telegram",
        text="", platform="telegram", user_id=update.effective_user.id,
        username=update.effective_user.username or "", instance_id=context.bot_data.get("instance_id"),
        attachment_path=rel_path, attachment_name=orig_name, attachment_mime=doc.mime_type,
    )
    if not (update.message.caption or "").strip():
        # require_auth's wrapper already pushed for a captioned document
        # (it logs+notifies on any non-empty text/caption) — this covers
        # the attachment-only case that leaves that path a no-op.
        asyncio.create_task(push.notify_new_message(context.bot_data.get("instance_name", "Bot"), f"📎 {orig_name}"))
    await _reply_chunked(update, f"Saved: {orig_name}. Reference it in your next /ask.", context)
