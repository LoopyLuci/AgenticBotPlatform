"""What every channel adapter does once it has an authorised sender's text: log it, run a slash command or ask the
agent, log and send the reply (roadmap P7). The adapters (email, SMS, Signal, iMessage) own how a message arrives
and how a reply leaves; this owns everything in between, so it is the same for all of them and the same as Slack's,
Discord's and WhatsApp's own copies of it.

Allow-listing stays in each adapter (what counts as "the same sender" differs: an e-mail address is compared
case-insensitively, a phone number by its digits) but goes through `is_allowed`, and a rejected sender is audited the
same way on every channel."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from bot import db, push
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.router import router

logger = logging.getLogger("bot.platforms.relay")

_sessions: dict[tuple, dict] = {}
Send = Callable[[str, str], Awaitable[None]]


def normalise_phone(value: str) -> str:
    """+15551234567 from "+1 (555) 123-4567"; a value with no digits comes back as ''."""
    digits = re.sub(r"\D", "", value or "")
    return f"+{digits}" if digits else ""


def normalise_email(value: str) -> str:
    return (value or "").strip().lower()


def is_allowed(instance: dict, sender: str, normalise: Callable[[str], str] = lambda s: s) -> bool:
    allowed = {normalise(str(i)) for i in instance["allowed_user_ids"]}
    return bool(sender) and normalise(sender) in allowed


def reject(instance: dict, platform: str, sender: str) -> None:
    logger.warning("rejected %s message from unauthorised sender %s on instance %r", platform, sender, instance["name"])
    db.log_audit(actor=sender, action="unauthorized_attempt", detail=f"{platform}:{sender} (instance {instance['id']})")


async def relay(instance: dict, platform: str, chat_id: str, sender: str, text: str, send: Send, *, username: str = "") -> None:
    """Handle one message from an already-authorised sender."""
    text = (text or "").strip()
    if not text:
        return
    db.log_message(platform=platform, chat_id=chat_id, user_id=sender, username=username, direction="in", source=platform,
                   text=text, instance_id=instance["id"])
    asyncio.create_task(push.notify_new_message(instance["name"], text))
    session = _sessions.setdefault((instance["id"], chat_id), {})
    ctx = CmdContext(instance_id=instance["id"], instance_name=instance["name"], user_id=sender, chat_id=chat_id, actor=sender, session=session)
    reply = await dispatch_command(text, ctx)
    if reply is None:
        try:
            result = await router.ask(text, action_type=session.get("action_type", "quick_question"), user_id=sender,
                                      context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                                      instance_id=instance["id"], chat_id=chat_id)
            reply = result.text
        except BackendError as exc:
            reply = f"Backend failed: {exc}"
    reply = reply or "(empty response)"
    db.log_message(platform=platform, chat_id=chat_id, direction="out", source="bot", text=reply, instance_id=instance["id"])
    await send(chat_id, reply)


def chunks(text: str, limit: int) -> list[str]:
    """Split a long reply at line breaks where possible."""
    out, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip() or not out:
        out.append(rest)
    return out
