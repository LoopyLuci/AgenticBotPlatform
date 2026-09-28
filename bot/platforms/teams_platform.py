"""Microsoft Teams (roadmap P7), through an Azure Bot: chat with the bot 1:1, or @mention it in a channel or group chat.

    credentials: app_id        the bot's Microsoft App ID (Azure Bot -> Configuration)
                 app_password  a client secret of that app registration
                 tenant_id     (optional) your Entra tenant ID, for a single-tenant bot; empty for a multi-tenant one
    allowed_user_ids: the Entra object IDs (GUIDs) of the people who may use it

Set the Azure Bot's messaging endpoint to `<public HTTPS address>/webhooks/teams` and add the Teams channel. The Bot Connector
POSTs each activity there with `Authorization: Bearer <JWT>`; it is verified as Microsoft documents
(learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-authentication):
- the signature, against the keys at login.botframework.com's OpenID document;
- issuer `https://api.botframework.com`;
- audience the App ID, and 5 minutes of clock skew;
- the token's `serviceUrl` claim equal to the activity's;
- when the signing key lists endorsements, the activity's channel among them.

Anything else gets 401 (403 for a missing endorsement).

The Connector expects a quick answer, so the webhook returns at once and the reply is posted afterwards to
`{serviceUrl}/v3/conversations/{id}/activities/{activity id}` with a token from Entra ID (client credentials, scope
`https://api.botframework.com/.default`). A typing indicator is sent while the agent works. Each conversation's service
address is remembered (data/teams_conversations.json) so scheduled and forwarded messages can reach it later.

**Not tested against a real Azure Bot or Teams tenant**; tested end to end against local stand-ins for Microsoft's key
server, token endpoint and Bot Connector, with real RS256 signatures, following the documented formats.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from bot import bot_instances
from bot.platforms import _jwt, _relay

logger = logging.getLogger("bot.platforms.teams")

ISSUER = "https://api.botframework.com"
SCOPE = "https://api.botframework.com/.default"
CONNECTOR_KEYS = _jwt.KeySet("https://login.botframework.com/v1/.well-known/openidconfiguration", discovery=True)
MAX_TEXT = 4000
_tokens = _jwt.AccessTokens()
_lock = threading.Lock()


def _store() -> Path:
    from bot import envfile

    return Path(envfile.PROJECT_ROOT) / "data" / "teams_conversations.json"


def remember(instance_id: int, conversation_id: str, service_url: str) -> None:
    with _lock:
        path = _store()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        key = f"{instance_id}:{conversation_id}"
        if data.get(key) == service_url:
            return
        data[key] = service_url
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)


def service_url_for(instance_id: int, conversation_id: str) -> Optional[str]:
    try:
        return json.loads(_store().read_text(encoding="utf-8")).get(f"{instance_id}:{conversation_id}")
    except (OSError, ValueError):
        return None


def clean_text(activity: dict) -> str:
    """The message without the bot's @mention and HTML: Teams sends `<at>Bot</at> hello`."""
    text = str(activity.get("text") or "")
    text = re.sub(r"<at>.*?</at>", " ", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ").strip()


async def check(activity: dict, authorization: str, *, client: Optional[httpx.AsyncClient] = None) -> tuple[Optional[dict], str, int]:
    """(instance, "", 200) for a verified message from an allowed person; otherwise (None, why, HTTP status to answer)."""
    try:
        token = _jwt.bearer(authorization)
    except _jwt.TokenError as exc:
        return None, str(exc), 401
    if not isinstance(activity, dict):
        return None, "not an activity", 400
    instance, why, status = None, "no Teams bot is switched on", 401
    for row in bot_instances.list_instances(platform="teams", enabled_only=True):
        try:
            claims, endorsements = await _jwt.verify(token, CONNECTOR_KEYS, audience=str(row["credentials"].get("app_id") or ""),
                                                     issuers=(ISSUER,), client=client)
        except _jwt.TokenError as exc:
            why = str(exc)
            continue
        if str(claims.get("serviceUrl") or "").rstrip("/") != str(activity.get("serviceUrl") or "").rstrip("/"):
            return None, "the token's serviceUrl does not match the activity's", 401
        if endorsements and activity.get("channelId") not in endorsements:
            return None, f"the signing key is not endorsed for channel {activity.get('channelId')!r}", 403
        instance = row
        break
    if instance is None:
        return None, why, status
    if activity.get("type") != "message":
        return None, f"a {activity.get('type')} activity (nothing to answer)", 200
    sender = activity.get("from") or {}
    ids = {str(sender.get("aadObjectId") or "").lower(), str(sender.get("id") or "").lower()} - {""}
    allowed = {str(i).strip().lower() for i in instance["allowed_user_ids"]}
    if not ids & allowed:
        _relay.reject(instance, "teams", str(sender.get("aadObjectId") or sender.get("id") or "?"))
        return None, "not allowed", 200
    return instance, "", 200


async def _token(instance: dict, client: httpx.AsyncClient) -> str:
    creds = instance["credentials"]
    return await _tokens.get(f"{creds['app_id']}@{creds.get('tenant_id') or ''}",
                             lambda: _jwt.microsoft_client_token(creds["app_id"], creds["app_password"],
                                                                 str(creds.get("tenant_id") or "").strip(), SCOPE, client))


async def post_activity(instance: dict, service_url: str, conversation_id: str, activity: dict, *, reply_to: str = "",
                        client: Optional[httpx.AsyncClient] = None) -> bool:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        token = await _token(instance, client)
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities" + (f"/{reply_to}" if reply_to else "")
        r = await client.post(url, json=activity, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            logger.warning("teams send failed for %r: %s %s", instance["name"], r.status_code, r.text[:200])
            return False
        return True
    except (_jwt.TokenError, httpx.HTTPError) as exc:
        logger.warning("teams send failed for %r: %s", instance["name"], exc)
        return False
    finally:
        if own:
            await client.aclose()


async def send_text(instance: dict, conversation_id: str, text: str, *, service_url: str = "", reply_to: str = "",
                    client: Optional[httpx.AsyncClient] = None) -> None:
    service_url = service_url or service_url_for(instance["id"], conversation_id) or ""
    if not service_url:
        logger.warning("teams: no service address known for conversation %s on %r", conversation_id, instance["name"])
        return
    for piece in _relay.chunks(text, MAX_TEXT):
        ok = await post_activity(instance, service_url, conversation_id, {"type": "message", "text": piece, "textFormat": "markdown"},
                                 reply_to=reply_to, client=client)
        if not ok:
            break


async def deliver(instance: dict, activity: dict, *, client: Optional[httpx.AsyncClient] = None) -> None:
    conversation = str((activity.get("conversation") or {}).get("id") or "")
    service_url = str(activity.get("serviceUrl") or "")
    remember(instance["id"], conversation, service_url)
    await post_activity(instance, service_url, conversation, {"type": "typing"}, client=client)
    sender = activity.get("from") or {}

    async def send(chat_id: str, reply: str) -> None:
        await send_text(instance, conversation, reply, service_url=service_url, reply_to=str(activity.get("id") or ""), client=client)

    await _relay.relay(instance, "teams", conversation, str(sender.get("aadObjectId") or sender.get("id") or ""),
                       clean_text(activity), send, username=str(sender.get("name") or ""))


async def run_instance(row: dict[str, Any]) -> None:
    """The supervisor's task: nothing to connect (Microsoft calls in); register the sender for scheduled and forwarded messages."""
    import asyncio

    from bot import outbox

    async def _send(chat_id: Any, text: str) -> None:
        await send_text(row, str(chat_id), text)

    outbox.register(row["id"], _send)
    try:
        await asyncio.Event().wait()
    finally:
        outbox.unregister(row["id"])
