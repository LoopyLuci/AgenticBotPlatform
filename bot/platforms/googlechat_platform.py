"""Google Chat (roadmap P7): message the bot in a Chat DM or @mention it in a space; the answer comes back in the thread.

    credentials: service_account_json   the JSON key of a Google Cloud service account (Chat API "Connection settings")
                 audience               what Chat puts in its token's audience: your project NUMBER, or the endpoint URL
    allowed_user_ids: e-mail addresses (or users/<id>) of the people who may use it

A Chat app configured with an "HTTP endpoint URL" POSTs every interaction event to `<public HTTPS address>/webhooks/googlechat`.
The endpoint cannot use the dashboard token, so each request's bearer token is verified the way Google documents
(developers.google.com/workspace/chat/verify-requests-from-chat), according to the app's "Authentication audience":

* **Project number**: a JWT signed by `chat@system.gserviceaccount.com`, checked against that account's published certificates,
  with the project number as audience.
* **HTTP endpoint URL**: a Google-signed ID token (issuer accounts.google.com) whose audience is the endpoint URL and
  whose `email` is `chat@system.gserviceaccount.com`.

Anything that does not verify gets 401. An agent turn outlasts Chat's 30-second response window, so the webhook answers
at once (an empty response posts nothing) and the reply is posted afterwards through the Chat API
(`spaces.messages.create`, scope `chat.bot`), in the thread the message came from, as the service account.

Only the classic "Chat app" configuration is handled. A Chat app built as a Google Workspace add-on sends a different
event envelope and token; such a request is refused with a message saying so. **Not tested against a real Google Chat app**
(no Google Workspace account was available); tested end to end against local stand-ins for Google's key server, token
endpoint and Chat API, with real RS256 signatures, following the documented formats.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from bot import bot_instances
from bot.platforms import _jwt, _relay

logger = logging.getLogger("bot.platforms.googlechat")

API = "https://chat.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/chat.bot"
CHAT_ISSUER = "chat@system.gserviceaccount.com"
PROJECT_KEYS = _jwt.KeySet(f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}")
GOOGLE_ID_KEYS = _jwt.KeySet("https://www.googleapis.com/oauth2/v1/certs")
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
MAX_TEXT = 4000                  # well under Chat's 32,000-byte message limit, and readable
_tokens = _jwt.AccessTokens()


def service_account(instance: dict) -> dict:
    try:
        data = json.loads(instance["credentials"].get("service_account_json") or "")
    except ValueError as exc:
        raise _jwt.TokenError("the service account key is not valid JSON") from exc
    if not isinstance(data, dict) or not data.get("client_email") or not data.get("private_key"):
        raise _jwt.TokenError("the service account key has no client_email or private_key")
    return data


def parse_event(body: dict) -> Optional[dict]:
    """The parts of a Chat interaction event ABP uses, or None for something that is not one."""
    if not isinstance(body, dict) or "type" not in body:
        return None
    msg = body.get("message") or {}
    user = body.get("user") or msg.get("sender") or {}
    space = body.get("space") or msg.get("space") or {}
    text = msg.get("argumentText") if msg.get("argumentText") is not None else msg.get("text")
    return {"type": str(body.get("type")), "text": (text or "").strip(), "space": str(space.get("name") or ""),
            "space_type": str(space.get("spaceType") or space.get("type") or ""),
            "thread": str((msg.get("thread") or {}).get("name") or ""),
            "user": str(user.get("name") or ""), "email": str(user.get("email") or "").lower(),
            "display_name": str(user.get("displayName") or ""), "user_type": str(user.get("type") or "HUMAN")}


async def _verify(token: str, audience: str, client: Optional[httpx.AsyncClient]) -> None:
    if audience.isdigit():
        await _jwt.verify(token, PROJECT_KEYS, audience=audience, issuers=(CHAT_ISSUER,), client=client)
        return
    claims, _ = await _jwt.verify(token, GOOGLE_ID_KEYS, audience=audience, issuers=GOOGLE_ISSUERS, client=client)
    if claims.get("email") != CHAT_ISSUER or claims.get("email_verified") is False:
        raise _jwt.TokenError("the ID token was not issued to Google Chat")


async def check(body: dict, authorization: str, *, client: Optional[httpx.AsyncClient] = None) -> tuple[Optional[dict], Optional[dict], str]:
    """(instance, event, "") for a verified event from an allowed person; (None, None, why) otherwise. `why` starting with
    "unauthorized" means the request itself failed verification (answer 401)."""
    if isinstance(body, dict) and "chat" in body and "commonEventObject" in body:
        return None, None, "unauthorized: this looks like a Google Workspace add-on event; configure the app as a Chat app"
    try:
        token = _jwt.bearer(authorization)
    except _jwt.TokenError as exc:
        return None, None, f"unauthorized: {exc}"
    instance, last_error = None, "no Google Chat bot is switched on"
    for row in bot_instances.list_instances(platform="googlechat", enabled_only=True):
        try:
            await _verify(token, str(row["credentials"].get("audience") or "").strip(), client)
        except _jwt.TokenError as exc:
            last_error = str(exc)
            continue
        instance = row
        break
    if instance is None:
        return None, None, f"unauthorized: {last_error}"
    event = parse_event(body)
    if event is None:
        return None, None, "not an interaction event"
    if event["user_type"] == "BOT":
        return None, None, "from a bot"
    allowed = {str(i).strip().lower() for i in instance["allowed_user_ids"]}
    if not ({event["email"], event["user"].lower()} & allowed) or not (event["email"] or event["user"]):
        _relay.reject(instance, "googlechat", event["email"] or event["user"])
        return None, None, "not allowed"
    return instance, event, ""


def to_chat_markup(text: str) -> str:
    """Chat formats *bold*, _italic_ and `code`; the agent writes Markdown's **bold**."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


async def send_text(instance: dict, space: str, text: str, *, thread: str = "", client: Optional[httpx.AsyncClient] = None) -> None:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        account = service_account(instance)
        token = await _tokens.get(account["client_email"],
                                  lambda: _jwt.google_service_account_token(account, SCOPE, client))
        params = {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"} if thread else {}
        for piece in _relay.chunks(to_chat_markup(text), MAX_TEXT):
            body: dict[str, Any] = {"text": piece}
            if thread:
                body["thread"] = {"name": thread}
            r = await client.post(f"{API}/{space}/messages", params=params, json=body, headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 300:
                logger.warning("google chat send failed for %r: %s %s", instance["name"], r.status_code, r.text[:200])
                break
    except (_jwt.TokenError, httpx.HTTPError) as exc:
        logger.warning("google chat send failed for %r: %s", instance["name"], exc)
    finally:
        if own:
            await client.aclose()


def welcome(event: dict) -> dict:
    """The synchronous answer when the app is added to a space or DM."""
    return {"text": "Hi! Message me here (in a space, @mention me) and I will answer. Only people on this bot's allowed list get answers."}


async def deliver(instance: dict, event: dict, *, client: Optional[httpx.AsyncClient] = None) -> None:
    async def send(chat_id: str, reply: str) -> None:
        await send_text(instance, event["space"], reply, thread=event["thread"], client=client)

    await _relay.relay(instance, "googlechat", event["space"], event["email"] or event["user"], event["text"], send,
                       username=event["display_name"])


async def run_instance(row: dict[str, Any]) -> None:
    """The supervisor's task: nothing to connect (Google calls in); register the sender for scheduled and forwarded messages."""
    import asyncio

    from bot import outbox

    async def _send(chat_id: Any, text: str) -> None:
        await send_text(row, str(chat_id), text)

    outbox.register(row["id"], _send)
    try:
        await asyncio.Event().wait()
    finally:
        outbox.unregister(row["id"])
