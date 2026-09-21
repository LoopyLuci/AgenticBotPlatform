"""iMessage (roadmap P7), through a BlueBubbles server running on a Mac you own.

    credentials: server_url (e.g. http://192.168.1.20:1234), password (the BlueBubbles server password),
                 webhook_token (any secret string, 16+ characters, that you also put in the webhook URL)
    allowed_user_ids: the phone numbers (+E.164) or Apple IDs (e-mail addresses) that may talk to it

iMessage has no API; the only way to send and receive it is a Mac signed in to iMessage. BlueBubbles is a server for that
Mac. In BlueBubbles add a webhook to `<your public HTTPS address>/webhooks/bluebubbles?token=<webhook_token>` for the "New
Message" event. This server checks the token (constant-time) because the endpoint cannot use the dashboard token, and
answers only allowed senders' direct messages. Replies go out through BlueBubbles' `POST /api/v1/message/text`.

Field names follow BlueBubbles' documented webhook and REST API from memory; **tested against a fake server written for
the tests, not against BlueBubbles or a Mac.** Group chats and messages you sent yourself are ignored.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from typing import Any, Optional

import httpx

from bot import bot_instances
from bot.platforms import _relay

logger = logging.getLogger("bot.platforms.imessage")
MAX_LEN = 4000


def normalise(address: str) -> str:
    """A phone number as +digits, an e-mail address in lower case."""
    return _relay.normalise_email(address) if "@" in (address or "") else _relay.normalise_phone(address)


def find_instance(token: str) -> Optional[dict]:
    for row in bot_instances.list_instances(platform="imessage", enabled_only=True):
        if hmac.compare_digest(str(row["credentials"].get("webhook_token", "")), token or ""):
            return row
    return None


def extract(payload: dict) -> Optional[tuple[str, str, str]]:
    """(sender, chat guid, text) for a new direct message from someone else, else None."""
    if payload.get("type") != "new-message":
        return None
    data = payload.get("data") or {}
    chats = data.get("chats") or []
    if data.get("isFromMe") or not data.get("text") or len(chats) != 1:
        return None
    guid = str(chats[0].get("guid") or "")
    if ";+;" in guid:                                    # BlueBubbles marks group chats with ";+;"; direct ones use ";-;"
        return None
    return normalise((data.get("handle") or {}).get("address", "")), guid, str(data["text"])


async def send_text(instance: dict, chat_guid: str, text: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
    creds = instance["credentials"]
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        for piece in _relay.chunks(text, MAX_LEN):
            r = await client.post(f"{creds['server_url'].rstrip('/')}/api/v1/message/text", params={"password": creds["password"]},
                                  json={"chatGuid": chat_guid, "tempGuid": str(uuid.uuid4()), "message": piece, "method": "apple-script"})
            if r.status_code >= 300:
                logger.warning("bluebubbles send failed for %r: %s", instance["name"], r.status_code)
                break
    finally:
        if own:
            await client.aclose()


def check(payload: dict, token: str) -> tuple[Optional[dict], tuple, str]:
    """(instance, (sender, chat guid, text), "") for a genuine message from an allowed sender, else (None, (), why)."""
    instance = find_instance(token)
    if instance is None:
        return None, (), "bad token"
    got = extract(payload)
    if got is None:
        return None, (), "ignored"
    if not _relay.is_allowed(instance, got[0], normalise):
        _relay.reject(instance, "imessage", got[0])
        return None, (), "not allowed"
    return instance, got, ""


async def deliver(instance: dict, got: tuple, *, client: Optional[httpx.AsyncClient] = None) -> None:
    sender, guid, text = got

    async def send(chat_id: str, reply: str) -> None:
        await send_text(instance, guid, reply, client=client)

    await _relay.relay(instance, "imessage", sender, sender, text, send)


async def handle_webhook(payload: dict, token: str, *, client: Optional[httpx.AsyncClient] = None) -> str:
    instance, got, why = check(payload, token)
    if instance is None:
        return why
    await deliver(instance, got, client=client)
    return "answered"


async def run_instance(row: dict[str, Any]) -> None:
    from bot import outbox

    async def _send(chat_id: Any, text: str) -> None:
        # A proactive message to an address: BlueBubbles' direct-chat guid is "iMessage;-;<address>".
        await send_text(row, f"iMessage;-;{normalise(str(chat_id))}", text)

    outbox.register(row["id"], _send)
    try:
        await asyncio.Event().wait()
    finally:
        outbox.unregister(row["id"])
