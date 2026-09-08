"""Server Chat's BotServer admin conversation pipeline — Section 3 of
the "Admin control surface" plan. The permanent group room doubles as
the channel you talk to BotServer in: any message posted there (per
the user's own choice of design) is routed through the designated
admin bot instance's own agent loop — the same admin_* tools a
Telegram admin bot gets, additionally gated by the SENDING DEVICE's own
permission_tier so ADMIN_TOOLS_ELEVATED only ever unlocks for an
elevated-or-higher device. Direct (1:1) device-to-device conversations
are never touched by this pipeline — pure peer messaging there stays
exactly as it always has been.

A dangerous-tool approval inside this pipeline posts a real
`kind="approval_request"` message into the same room (see
bot/db.py's server_chat_messages schema) instead of a Telegram inline
keyboard — resolved through the exact same bot.agent_runtime.approval
state machine via POST /api/server-chat/approvals/{id}/resolve.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("bot.server_chat_admin")


def resolve_device_tier(device_id: int) -> str:
    """The desktop sentinel is the unconditional top authority (matches
    every other admin-surface enforcement point in this plan); any other
    device's tier comes straight from its own api_keys row, defaulting
    to "none" for a revoked/missing key rather than raising."""
    from bot import db

    if device_id == db.SERVER_CHAT_DESKTOP_DEVICE_ID:
        return "unrestricted"
    row = db.get_api_key(device_id)
    return row["permission_tier"] if row else "none"


async def maybe_handle_group_message(conversation_id: int, sender_device_id: int, text: str) -> None:
    """Called synchronously from api_server_chat_send AFTER the human's
    own message is already stored — never raises into the caller (a
    pipeline failure must never make an ordinary Server Chat send fail;
    the human's message is already safely saved by the time this runs)."""
    from bot import agent_settings, db

    try:
        conv = db.get_conn().execute(
            "SELECT kind FROM server_chat_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if conv is None or conv["kind"] != "group":
            return  # direct (1:1) conversations are pure peer messaging, always
        if sender_device_id == db.SERVER_CHAT_BOT_DEVICE_ID:
            return  # never react to our own reply

        admin_instance_id = agent_settings.get_admin_instance_id()
        if admin_instance_id is None:
            return  # no admin instance configured — stays pure peer messaging

        from bot.router import router

        tier = resolve_device_tier(sender_device_id)
        session_key = f"serverchat:{conversation_id}"

        async def notify(approval_id: int, tool_name: str, tool_input: dict) -> None:
            summary = f"📋 Approval needed: {tool_name}({tool_input}) — reply with Approve or Deny."
            db.create_server_chat_message(
                conversation_id, db.SERVER_CHAT_BOT_DEVICE_ID, summary,
                kind="approval_request", approval_id=approval_id,
            )

        result = await router.ask(
            text,
            instance_id=admin_instance_id,
            action_type="server_chat_admin",
            context={
                "desktop_session_key": session_key,
                "chat_id": session_key,
                "device_tier": tier,
                "approval_notify": notify,
            },
        )
        reply_text = (result.text or "").strip()
        if reply_text:
            db.create_server_chat_message(conversation_id, db.SERVER_CHAT_BOT_DEVICE_ID, reply_text)
    except Exception:
        logger.exception("server chat admin pipeline failed for conversation %s", conversation_id)
