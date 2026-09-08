"""Support Bot orchestration: classify -> extract slots -> confirm-gate
destructive actions -> execute. The one place that ties model.py,
slots.py, and actions.py together into a single request/reply call.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from bot import commands as bot_commands
from bot.config import config
from bot.support_bot import actions
from bot.support_bot import hybrid
from bot.support_bot.training_data import DESTRUCTIVE_INTENTS

# Pending confirmations expire quickly — this mirrors a chat confirm
# prompt, not a durable queue; a stale token just means "ask again."
CONFIRM_TTL_S = 300


@dataclass
class SupportBotReply:
    text: str
    intent: str
    needs_confirm: bool = False
    confirm_token: Optional[str] = None
    applied: bool = False


class SupportBot:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, str, str, float]] = {}  # token -> (intent, text, actor, created_at)
        # A literal slash command from the Support Bot panel runs through
        # the exact same dispatch_command() Telegram/Discord/Slack use
        # (bot/commands.py) rather than the NLP classifier — same commands,
        # same behavior, everywhere. One shared session since the desktop
        # panel is a single ongoing conversation (mirrors how each
        # Discord/Slack chat keeps one CmdContext.session per channel).
        self._cmd_session: dict = {}

    def _confirm_required(self, intent: str) -> bool:
        if intent not in DESTRUCTIVE_INTENTS:
            return False
        return bool((config.current.get("security") or {}).get("confirm_destructive", True))

    async def _execute(self, intent: str, text: str, actor: str) -> str:
        if intent in actions.ASYNC_INTENT_HANDLERS:
            return await actions.ASYNC_INTENT_HANDLERS[intent](text, actor)
        return actions.INTENT_HANDLERS[intent](text, actor)

    async def handle(self, text: str, actor: str, client_intent: Optional[str] = None) -> SupportBotReply:
        """`client_intent` (Phase 6 of the Support Bot NLU upgrade plan)
        is an OPTIONAL fast-path hint from a caller that already ran its
        own local classification (the Android app's on-device Kotlin
        engine) — used only when it names a real, currently-registered
        intent; anything else (a stale/unrecognized value, an "unknown"
        guess, or its absence) falls through to this server's own real
        classification exactly as before this parameter existed. Every
        downstream step — the confirm-gate for destructive intents,
        actual execution — runs identically either way, so a client
        can never get a destructive action to skip approval just by
        asserting an intent; it can only ever save a round-trip to
        recompute what text.classify() would have said anyway."""
        text = (text or "").strip()
        if not text:
            return SupportBotReply(text="Say something and I'll help — try \"help\" for what I can do.", intent="unknown")

        if text.startswith("/"):
            ctx = bot_commands.CmdContext(
                instance_id=None,
                instance_name="Support Bot",
                user_id="dashboard",
                chat_id="support-bot",
                actor=actor,
                session=self._cmd_session,
            )
            reply_text = await bot_commands.dispatch_command(text, ctx)
            if reply_text is not None:
                cmd = text[1:].split(None, 1)[0].lower()
                return SupportBotReply(text=reply_text, intent=f"slash:{cmd}", applied=True)
            return SupportBotReply(
                text=f"Unknown command {text.split()[0]!r} — type \"/\" to see the list.",
                intent="unknown",
            )

        valid_intents = set(actions.INTENT_HANDLERS) | set(actions.ASYNC_INTENT_HANDLERS)
        if client_intent and client_intent in valid_intents:
            intent = client_intent
        else:
            intent = hybrid.classify(text).intent
        if intent == "unknown":
            return SupportBotReply(
                text="I'm not sure what you're asking — try \"help\" to see what I can do.",
                intent="unknown",
            )

        if self._confirm_required(intent):
            token = uuid.uuid4().hex
            self._pending[token] = (intent, text, actor, time.time())
            return SupportBotReply(
                text=f"This will {intent.replace('_', ' ')} — confirm?",
                intent=intent,
                needs_confirm=True,
                confirm_token=token,
            )

        try:
            reply_text = await self._execute(intent, text, actor)
        except actions.ActionError as exc:
            return SupportBotReply(text=str(exc), intent=intent)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as plain text, never a stack trace
            return SupportBotReply(text=f"That failed: {exc}", intent=intent)
        return SupportBotReply(text=reply_text, intent=intent, applied=True)

    async def confirm(self, token: str, actor: str) -> SupportBotReply:
        pending = self._pending.pop(token, None)
        if pending is None:
            return SupportBotReply(text="That confirmation expired or was already used — ask again.", intent="unknown")
        intent, text, orig_actor, created_at = pending
        if time.time() - created_at > CONFIRM_TTL_S:
            return SupportBotReply(text="That confirmation expired — ask again.", intent=intent)
        try:
            reply_text = await self._execute(intent, text, actor)
        except actions.ActionError as exc:
            return SupportBotReply(text=str(exc), intent=intent)
        except Exception as exc:  # noqa: BLE001
            return SupportBotReply(text=f"That failed: {exc}", intent=intent)
        return SupportBotReply(text=reply_text, intent=intent, applied=True)


support_bot = SupportBot()
