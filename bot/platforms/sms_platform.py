"""SMS through Twilio (roadmap P7): text a number, get the answer back as a text.

    credentials: account_sid (AC...), auth_token, from_number (the Twilio number, +E.164)
    allowed_user_ids: the phone numbers (+E.164) that may talk to it

Like WhatsApp this is a webhook: Twilio POSTs each inbound text to `<your public HTTPS address>/webhooks/sms` (set it
as the number's "A message comes in" webhook). Twilio signs every request (`X-Twilio-Signature`: HMAC-SHA1 of the exact
URL plus the sorted form fields, keyed by your auth token); a request that does not verify is refused, because this
endpoint cannot use the dashboard token. Behind a proxy the URL Twilio signed may differ from the one this server
sees, so set `sms.public_url` (the exact URL configured at Twilio) in config/backends.yaml.

Replies are sent through Twilio's REST API in pieces of at most 1500 characters; each piece is billed by Twilio as
one or more segments, so long answers cost money. Only allowed numbers are answered; anything else is audited and
dropped without a reply (a reply would confirm the number is live).

Tested against a faked Twilio (signature algorithm implemented from Twilio's documented description, requests captured);
**not tested with a real Twilio account.**
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from bot import bot_instances
from bot.platforms import _relay

logger = logging.getLogger("bot.platforms.sms")

API = "https://api.twilio.com/2010-04-01"
SEGMENT = 1500


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("sms")) or {}
    except Exception:  # noqa: BLE001
        return {}


def signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio's request signature: base64(HMAC-SHA1(token, url + each key and value, keys sorted))."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def verify_signature(auth_token: str, url: str, params: dict, given: str) -> bool:
    return bool(given) and hmac.compare_digest(signature(auth_token, url, params), given)


def find_instance(to_number: str) -> Optional[dict]:
    want = _relay.normalise_phone(to_number)
    for row in bot_instances.list_instances(platform="sms", enabled_only=True):
        if _relay.normalise_phone(row["credentials"].get("from_number", "")) == want:
            return row
    return None


async def send_text(instance: dict, to: str, text: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
    creds = instance["credentials"]
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        for piece in _relay.chunks(text, SEGMENT):
            r = await client.post(f"{API}/Accounts/{creds['account_sid']}/Messages.json", auth=(creds["account_sid"], creds["auth_token"]),
                                  data={"From": creds["from_number"], "To": to, "Body": piece})
            if r.status_code >= 300:
                logger.warning("twilio send failed for %r: %s %s", instance["name"], r.status_code, r.text[:200])
                break
    finally:
        if own:
            await client.aclose()


def check(form: dict, url: str, given_signature: str) -> tuple[Optional[dict], str, str]:
    """(instance, sender, "") for a genuine text from an allowed number, else (None, "", why). Fast and synchronous, so a webhook
    can refuse a forged or stray request before doing any work; the answer itself is produced afterwards by `deliver`."""
    instance = find_instance(form.get("To", ""))
    if instance is None:
        return None, "", "unknown number"
    public = str(_cfg().get("public_url") or "").strip()
    if not verify_signature(instance["credentials"]["auth_token"], public or url, form, given_signature):
        return None, "", "bad signature"
    sender = _relay.normalise_phone(form.get("From", ""))
    if not _relay.is_allowed(instance, sender, _relay.normalise_phone):
        _relay.reject(instance, "sms", sender)
        return None, "", "not allowed"
    return instance, sender, ""


async def deliver(instance: dict, sender: str, body: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
    async def send(chat_id: str, reply: str) -> None:
        await send_text(instance, sender, reply, client=client)

    await _relay.relay(instance, "sms", sender, sender, body, send)


async def handle_webhook(form: dict, url: str, given_signature: str, *, client: Optional[httpx.AsyncClient] = None) -> str:
    """check + deliver in one call. Returns "answered" or why not; `given_signature` is the X-Twilio-Signature header."""
    instance, sender, why = check(form, url, given_signature)
    if instance is None:
        return why
    await deliver(instance, sender, form.get("Body", ""), client=client)
    return "answered"


async def run_instance(row: dict[str, Any]) -> None:
    """The supervisor's task: nothing to connect (see the module docstring); register the outbound sender and wait."""
    import asyncio

    from bot import outbox

    async def _send(chat_id: Any, text: str) -> None:
        await send_text(row, _relay.normalise_phone(str(chat_id)), text)

    outbox.register(row["id"], _send)
    try:
        await asyncio.Event().wait()
    finally:
        outbox.unregister(row["id"])
