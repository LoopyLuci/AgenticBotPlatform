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
import itertools
import logging
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import attachments, bot_instances, commands, db, desktop, pairing, push, slash_access, slash_commands
from bot.agent_runtime import approval as agent_approval
from bot.agent_runtime import engine as agent_engine
from bot.commands import CmdContext
from bot.config import config

logger = logging.getLogger("bot.handlers")

TELEGRAM_MAX_LEN = 4096


async def _maybe_offer_pairing(update: Update, context: ContextTypes.DEFAULT_TYPE, user, instance_id) -> None:
    """Real Hermes-style pairing UX for an unrecognized DM sender: reply
    with a one-time code instead of silence, so a legitimate new user has a
    way in. Groups stay silent — a bot getting spammed with pairing-code
    replies by strangers in a public group is worse than just being
    ignored there, same call Hermes makes."""
    chat = update.effective_chat
    if chat is None or chat.type != "private" or instance_id is None:
        return
    ok, result = pairing.request_code(instance_id, user.id, user.username or user.first_name or "", chat.id)
    if ok:
        text = (
            "Hi — I don't recognize you yet.\n\n"
            f"Your pairing code: `{result}`\n\n"
            "Ask the bot owner to approve it from the dashboard's Pairing panel. "
            f"Code expires in {pairing.TTL_SECONDS // 60} minutes."
        )
    else:
        text = result
    try:
        await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("failed to send pairing reply to unauthorized user %s", user.id)


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
                await _maybe_offer_pairing(update, context, user, instance_id)
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


def _approval_keyboard(approval_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Allow Once", callback_data=f"ea:once:{approval_id}"),
                InlineKeyboardButton("✅ Session", callback_data=f"ea:session:{approval_id}"),
            ],
            [
                InlineKeyboardButton("✅ Always", callback_data=f"ea:always:{approval_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"ea:deny:{approval_id}"),
            ],
        ]
    )


def _notify_approval(context: ContextTypes.DEFAULT_TYPE, chat_id):
    """Builds the CmdContext.notify_approval closure for one Telegram
    chat — sends the ea: button message the agent-loop engine waits on
    (bot/agent_runtime/approval.py), matching the real Hermes Agent's own
    exec-approval message shape."""

    async def _notify(approval_id: int, tool_name: str, tool_input: dict) -> None:
        command = tool_input.get("command") if tool_name == "run_shell" else None
        body = f"⚠️ Approval required: {tool_name}"
        if command:
            body += f"\n\n`{command[:1000]}`"
        elif tool_name == "write_file":
            body += f"\n\nPath: {tool_input.get('path')!r} ({len(tool_input.get('content', ''))} chars)"
        await context.bot.send_message(
            chat_id=chat_id, text=body, parse_mode=ParseMode.MARKDOWN, reply_markup=_approval_keyboard(approval_id)
        )

    return _notify


def _ctx_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> CmdContext:
    chat_id = update.effective_chat.id
    msg = update.effective_message
    thread_id = getattr(msg, "message_thread_id", None) if msg is not None else None
    return CmdContext(
        instance_id=context.bot_data.get("instance_id"),
        instance_name=context.bot_data.get("instance_name", "Bot"),
        user_id=update.effective_user.id,
        chat_id=chat_id,
        actor=str(update.effective_user.id),
        session=context.user_data,
        notify_approval=_notify_approval(context, chat_id),
        thread_id=thread_id,
    )


# --------------------------------------------------------------- basic ----
# Every function below (until on_command/on_callback/on_document) is a
# per-command implementation looked up by TELEGRAM_HANDLERS and invoked from
# inside on_command — it does NOT carry its own @require_auth, since
# on_command is the single choke point that already checked it once for
# this update (mirrors Hermes's single-entry-point dispatch).

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_help(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


async def cmd_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    await _reply_chunked(update, slash_commands.commands_page(page), context)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args) if context.args else ""
    await _handle_ask(update, context, raw)


@require_auth
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_ask(update, context, update.message.text or "")


async def _set_reaction(context: ContextTypes.DEFAULT_TYPE, msg, emoji: Optional[str]) -> None:
    try:
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id, message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji)] if emoji else [],
        )
    except Exception:
        pass  # reactions are cosmetic — a bot without the permission, or an old Bot API, shouldn't break /ask


async def _handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    await update.message.chat.send_action("typing")
    msg = update.message
    await _set_reaction(context, msg, "👀")

    status_msg = None

    async def progress(text: str) -> None:
        nonlocal status_msg
        if status_msg is None:
            status_msg = await msg.reply_text(text)
        else:
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass  # e.g. Telegram's "message not modified" if two tool calls produce the same line back to back

    ctx = _ctx_from(update, context)
    ctx.progress_notify = progress
    reply = await commands.cmd_ask(ctx, raw)

    if status_msg is not None:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if reply == "Stopped.":
        await _set_reaction(context, msg, None)
    else:
        await _set_reaction(context, msg, "👎" if reply.startswith("Backend failed:") else "👍")

    await _reply_chunked(update, reply, context)


# --------------------------------------------------------------- status ---

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_status(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# -------------------------------------------------------------- models ----
# /model with no args opens an inline-keyboard picker scoped to THIS bot's
# own connected backend only — not a chooser across every model backend
# BotServer supports regardless of what this bot actually uses. Reads and
# writes bot_instances.model (the same per-instance override field the
# dashboard's own per-bot Model dropdown edits), via
# commands.instance_model_page()/apply_instance_model(). Discord/Slack keep
# the plain-text commands.cmd_model form (see bot/commands.py) — a
# *global* per-backend-family default, a different and still-useful
# setting, since neither platform integration here builds inline-keyboard
# widgets anyway.

def _paired_rows(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """2-per-row layout, matching the real Hermes Agent Telegram bot's
    picker keyboards — a single 4+-button row truncates awkwardly on
    mobile, per the same reasoning Hermes's own adapter documents."""
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


_NO_PROVIDER = -1  # sentinel provider index for a single-group backend (no picking needed)


async def _model_providers_page(instance_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Top level of the two-level picker for a backend with more than one
    model provider (only Hermes backends can have this — Hermes's own
    cache is keyed by provider, e.g. openrouter/anthropic/opencode-zen —
    the Claude family always has exactly one and skips straight to the
    model list)."""
    data = await commands.instance_model_page(instance_id, None, 0)
    if data is None:
        return "This chat isn't linked to a bot instance.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")]]
        )
    if data["mode"] == "models":
        # Only one provider group (or none) — no picking needed, go straight in.
        return await _model_page(instance_id, data.get("provider_idx", _NO_PROVIDER), 0)
    buttons = []
    for p in data["providers"]:
        mark = "✓ " if p["is_current"] else ""
        free = f", {p['free_count']} free" if p["free_count"] else ""
        buttons.append(InlineKeyboardButton(
            f"{mark}{p['name']} ({p['count']}{free})",
            callback_data=f"modelpick:provider:{instance_id}:{p['idx']}",
        ))
    rows = [[b] for b in buttons]
    rows.append([InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")])
    text = f"Model provider for this bot ({data['backend']}) — current: {data['current_model'] or '(backend default)'}"
    return text, InlineKeyboardMarkup(rows)


async def _model_page(instance_id: int, provider_idx: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    provider = None if provider_idx == _NO_PROVIDER else provider_idx
    data = await commands.instance_model_page(instance_id, provider, page)
    rows = []
    if data is None:
        return "This chat isn't linked to a bot instance.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")]]
        )
    backend = data["backend"]
    idx = data.get("provider_idx")
    idx = _NO_PROVIDER if idx is None else idx
    if data["has_known_list"]:
        buttons = []
        for m in data["models"]:
            mark = "✓ " if m == data["current_model"] else ""
            free = " 🆓" if commands.is_free_model_id(m) else ""
            buttons.append(InlineKeyboardButton(f"{mark}{m}{free}", callback_data=f"modelpick:set:{instance_id}:{m}"))
        rows.extend(_paired_rows(buttons))
        nav = []
        if data["page"] > 0:
            nav.append(InlineKeyboardButton("< Prev", callback_data=f"modelpick:page:{instance_id}:{idx}:{data['page'] - 1}"))
        if data["page"] < data["total_pages"] - 1:
            nav.append(InlineKeyboardButton("Next >", callback_data=f"modelpick:page:{instance_id}:{idx}:{data['page'] + 1}"))
        if nav:
            rows.append(nav)
        if data.get("multi_provider"):
            rows.append([InlineKeyboardButton("< Providers", callback_data=f"modelpick:providers:{instance_id}")])
        provider_label = f" — {data['provider']}" if data.get("provider") else ""
        text = f"Model for this bot ({backend}{provider_label}) — current: {data['current_model'] or '(backend default)'}"
    else:
        text = (
            f"This bot's backend ({backend}) has no discoverable model list.\n"
            f"Reply with `/model <name>` to set one directly.\n"
            f"Current: {data['current_model'] or '(backend default)'}"
        )
    rows.append([InlineKeyboardButton("Cancel", callback_data="modelpick:cancel")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from(update, context)
    args = context.args or []

    if not args or (len(args) == 1 and args[0] not in ("show", "set")):
        if len(args) == 1:
            # Bare `/model <name>` — sets this bot's model directly instead
            # of opening the picker, same shorthand Hermes offers.
            if ctx.instance_id is None:
                await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
                return
            reply = commands.apply_instance_model(ctx.instance_id, args[0], actor=ctx.actor)
            await _reply_chunked(update, reply, context)
            return
        if ctx.instance_id is None:
            await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
            return
        text, kb = await _model_providers_page(ctx.instance_id)
        await update.message.reply_text(text, reply_markup=kb)
        return

    if args[0] == "show":
        if ctx.instance_id is None:
            await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
            return
        instance = bot_instances.get_instance(ctx.instance_id)
        await _reply_chunked(update, f"{instance['backend']}: {instance.get('model') or '(backend default)'}", context)
        return
    if args[0] == "set" and len(args) >= 2:
        if ctx.instance_id is None:
            await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
            return
        model = " ".join(args[1:]).strip()
        reply = commands.apply_instance_model(ctx.instance_id, model, actor=ctx.actor)
        await _reply_chunked(update, reply, context)
        return

    await _reply_chunked(update, "Usage: /model | /model <name> | /model show | /model set <name>", context)


# ---------------------------------------------------------- desktop ctrl --

def _confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm", callback_data=f"confirm:{action}"),
          InlineKeyboardButton("Cancel", callback_data="cancel")]]
    )


async def cmd_start_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        desktop.start()
        await _reply_chunked(update, "Claude Desktop starting.", context)
    except FileNotFoundError as exc:
        await _reply_chunked(update, str(exc), context)


async def cmd_stop_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (config.current.get("security") or {}).get("confirm_destructive", True):
        await update.message.reply_text("Stop Claude Desktop?", reply_markup=_confirm_keyboard("stop_desktop"))
    else:
        desktop.stop()
        await _reply_chunked(update, "Claude Desktop stopped.", context)


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
    if data.startswith("modelpick:providers:"):
        _, _, instance_id_s = data.split(":", 2)
        text, keyboard = await _model_providers_page(int(instance_id_s))
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("modelpick:provider:"):
        _, _, instance_id_s, provider_idx_s = data.split(":", 3)
        text, keyboard = await _model_page(int(instance_id_s), int(provider_idx_s), 0)
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("modelpick:page:"):
        _, _, instance_id_s, provider_idx_s, page = data.split(":", 4)
        text, keyboard = await _model_page(int(instance_id_s), int(provider_idx_s), int(page))
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("modelpick:set:"):
        _, _, instance_id_s, model = data.split(":", 3)
        reply = commands.apply_instance_model(int(instance_id_s), model, actor=str(update.effective_user.id))
        await query.edit_message_text(reply)
        return
    if data.startswith("session:resume:"):
        chat_session_id = int(data.split(":", 2)[2])
        reply = await commands.cmd_resume(_ctx_from(update, context), [str(chat_session_id)])
        await query.edit_message_text(reply)
        return
    if data.startswith("ea:"):
        _, outcome, approval_id_s = data.split(":", 2)
        approval_id = int(approval_id_s)
        actor = str(update.effective_user.id)
        ok = agent_approval.resolve(approval_id, outcome, actor=actor)
        if not ok:
            await query.answer("This approval was already resolved.", show_alert=True)
            return
        label = {"once": "✅ Approved once", "session": "✅ Approved for session",
                  "always": "✅ Approved permanently", "deny": "❌ Denied"}[outcome]
        await query.edit_message_text(f"{label} by {update.effective_user.first_name or actor}")
        return
    if data.startswith("sc:"):
        _, outcome, cid_s = data.split(":", 2)
        entry = _slash_confirm_pending.pop(int(cid_s), None)
        if entry is None:
            await query.answer("This confirmation already expired.", show_alert=True)
            return
        action, key = entry
        actor = update.effective_user.first_name or str(update.effective_user.id)
        if outcome == "cancel":
            await query.edit_message_text(f"Cancelled by {actor}")
            return
        if outcome == "always":
            _slash_confirm_always.add(key)
        result_text = await action()
        label = "✅ Approved once" if outcome == "once" else "🔒 Always approve"
        await query.edit_message_text(f"{label} by {actor}\n\n{result_text}")
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

async def cmd_backend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_backend(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ----------------------------------------------------------------- mcp ----

async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_mcp(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ------------------------------------------------------------- project ----

async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_project(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# --------------------------------------------------------- new session ----
# sc: slash-confirm — the real Hermes Agent's own inline-keyboard flow for
# a destructive slash command (/new interrupting an in-flight turn), used
# here instead of the plain-text "/new confirm" fallback commands.py's
# cmd_new_session offers Discord/Slack. Pending confirmations are
# in-memory only (a monotonic counter, same shape as approval.py's) since
# there's exactly one BotServer process; "always" is remembered per
# (instance, chat, thread) for the rest of this process's run, not
# persisted — a fresh restart asks again once, which is a fine default.

_slash_confirm_pending: dict[int, tuple] = {}
_slash_confirm_counter = itertools.count(1)
_slash_confirm_always: set[tuple] = set()


def _register_slash_confirm(action, key: tuple) -> int:
    cid = next(_slash_confirm_counter)
    _slash_confirm_pending[cid] = (action, key)
    return cid


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from(update, context)
    key = (ctx.instance_id, ctx.chat_id, ctx.thread_id)
    if (
        ctx.instance_id is not None
        and key not in _slash_confirm_always
        and agent_engine.is_running(ctx.instance_id, ctx.chat_id, ctx.thread_id)
    ):
        cid = _register_slash_confirm(lambda: commands.cmd_new_session(ctx, ["confirm"]), key)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve Once", callback_data=f"sc:once:{cid}"),
                    InlineKeyboardButton("🔒 Always Approve", callback_data=f"sc:always:{cid}"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"sc:cancel:{cid}")],
            ]
        )
        await update.message.reply_text(
            "A message is still in flight for this chat. Start a new session anyway?", reply_markup=kb
        )
        return
    reply = await commands.cmd_new_session(ctx, context.args or [])
    await _reply_chunked(update, reply, context)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _ctx_from(update, context)
    if ctx.instance_id is None:
        await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
        return
    rows = db.list_chat_sessions(ctx.instance_id, chat_id=ctx.chat_id, limit=20)
    if not rows:
        await _reply_chunked(update, "No linked sessions yet for this chat — use /new to create one.", context)
        return
    lines = ["Sessions for this chat (newest first):"]
    kb_rows = []
    for r in rows:
        mark = "* " if r["archived_at"] is None else "  "
        label = r["title"] or r["desktop_session_key"]
        lines.append(f"{mark}#{r['id']} {label} — linked {r['created_at']}")
        if r["archived_at"] is not None:
            kb_rows.append([InlineKeyboardButton(f"Resume #{r['id']}", callback_data=f"session:resume:{r['id']}")])
    markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_resume(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


async def cmd_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_title(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = await commands.cmd_profile(_ctx_from(update, context), context.args or [])
    await _reply_chunked(update, reply, context)


# ------------------------------------------------------------- whoami -----

def _scope_of(update: Update) -> str:
    chat = update.effective_chat
    return "dm" if (chat and chat.type == "private") else "group"


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instance_id = context.bot_data.get("instance_id")
    instance = bot_instances.get_instance(instance_id) if instance_id is not None else None
    if instance is None:
        await _reply_chunked(update, "This chat isn't linked to a bot instance.", context)
        return
    user = update.effective_user
    scope = _scope_of(update)
    t = slash_access.tier(instance, user.id)
    lines = [
        f"You — telegram ({scope})",
        f"User ID: {user.id}",
        f"Tier: {t}",
    ]
    if t == "unrestricted":
        lines.append("Slash commands: all available (no admin list configured for this bot)")
    elif t == "admin":
        lines.append("Slash commands: all available")
    else:
        allowed = sorted(slash_access.allowed_commands(instance, user.id, scope) or [])
        lines.append("Slash commands you can run: " + ", ".join(f"/{c}" for c in allowed))
    await _reply_chunked(update, "\n".join(lines), context)


# ------------------------------------------------------------ dispatch ----
# Single entry point for every /command update — resolves aliases through
# the shared registry (bot/slash_commands.py) and either runs a Telegram-
# specific rich handler (inline-keyboard confirms, typing indicator, etc.)
# or falls back to the plain-text platform-agnostic implementation in
# bot/commands.py. This is the only place command updates get auth-checked
# (see the comment above cmd_start).

TELEGRAM_HANDLERS: dict[str, Callable] = {
    "start": cmd_start,
    "help": cmd_start,
    "ask": cmd_ask,
    "status": cmd_status,
    "backend": cmd_backend,
    "model": cmd_model,
    "mcp": cmd_mcp,
    "project": cmd_project,
    "new": cmd_new,
    "sessions": cmd_sessions,
    "resume": cmd_resume,
    "title": cmd_title,
    "profile": cmd_profile,
    "whoami": cmd_whoami,
    "commands": cmd_commands,
    "start_desktop": cmd_start_desktop,
    "stop_desktop": cmd_stop_desktop,
    "restart_desktop": cmd_restart_desktop,
}


@require_auth
async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    body = text[1:] if text.startswith("/") else text
    cmd_token, *rest = (body.split(None, 1) or [""])
    cmd_raw = cmd_token.split("@", 1)[0].lower()
    args_text = rest[0] if rest else ""
    context.args = args_text.split() if args_text else []

    canonical = slash_commands.resolve_command(cmd_raw)
    if canonical is None:
        await _reply_chunked(update, f"Unknown command: /{cmd_raw}\nUse /help to see available commands.", context)
        return

    instance_id = context.bot_data.get("instance_id")
    instance = bot_instances.get_instance(instance_id) if instance_id is not None else None
    if instance is not None and not slash_access.can_run(instance, update.effective_user.id, _scope_of(update), canonical):
        await _reply_chunked(
            update, f"⛔ You are not authorized to run /{canonical}. Use /whoami to see what you can run.", context
        )
        return

    handler = TELEGRAM_HANDLERS.get(canonical)
    if handler is not None:
        await handler(update, context)
        return

    reply = await commands.dispatch_command(f"/{canonical} {args_text}".strip(), _ctx_from(update, context))
    await _reply_chunked(update, reply if reply is not None else "Not implemented yet.", context)


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
