"""Signal (roadmap P7), through a signal-cli REST bridge you run yourself.

    credentials: api_url (e.g. http://localhost:8080), number (the bot's own registered Signal number, +E.164)
    allowed_user_ids: the phone numbers (+E.164) that may talk to it

ABP does not speak Signal's protocol. It talks to the `signal-cli-rest-api` project (a container wrapping signal-cli),
which you register with a spare number: the bridge holds the Signal keys. ABP polls `GET /v1/receive/<number>` every
`signal.poll_interval_s` (2) seconds and sends with `POST /v2/send`. Direct messages from allowed numbers are answered;
group messages are ignored. Messages are end-to-end encrypted to the bridge, which sees them in the clear - run it on
a machine you trust.

The request and response shapes follow the bridge's documented API, from memory of it; **tested against a fake bridge
written for the tests, not against a real signal-cli-rest-api**. The bridge must run in `normal` or `native` mode (the
`json-rpc` mode delivers messages over a websocket instead, which is not supported here).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from bot.platforms import _relay

logger = logging.getLogger("bot.platforms.signal")
MAX_LEN = 4000


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("signal")) or {}
    except Exception:  # noqa: BLE001
        return {}


def extract(envelope: dict) -> Optional[tuple[str, str]]:
    """(sender number, text) for a direct text message, else None (groups, receipts, typing, reactions...)."""
    env = envelope.get("envelope") or envelope
    data = env.get("dataMessage")
    if not isinstance(data, dict) or data.get("groupInfo") or data.get("groupV2") or not data.get("message"):
        return None
    sender = env.get("sourceNumber") or env.get("source") or ""
    return _relay.normalise_phone(sender), str(data["message"])


async def send_text(creds: dict, to: str, text: str, client: httpx.AsyncClient) -> None:
    for piece in _relay.chunks(text, MAX_LEN):
        r = await client.post(f"{creds['api_url'].rstrip('/')}/v2/send", json={"message": piece, "number": creds["number"], "recipients": [to]})
        if r.status_code >= 300:
            logger.warning("signal send failed: %s %s", r.status_code, r.text[:200])
            break


async def poll_once(instance: dict, client: httpx.AsyncClient) -> int:
    """Fetch and handle waiting messages. Returns how many were answered."""
    creds = instance["credentials"]
    r = await client.get(f"{creds['api_url'].rstrip('/')}/v1/receive/{creds['number']}")
    if r.status_code >= 300:
        raise RuntimeError(f"signal bridge answered {r.status_code}")
    answered = 0
    for envelope in r.json() or []:
        got = extract(envelope)
        if got is None:
            continue
        sender, text = got
        if not _relay.is_allowed(instance, sender, _relay.normalise_phone):
            _relay.reject(instance, "signal", sender)
            continue

        async def send(chat_id: str, reply: str, _s: str = sender) -> None:
            await send_text(creds, _s, reply, client)

        await _relay.relay(instance, "signal", sender, sender, text, send)
        answered += 1
    return answered


async def run_instance(row: dict[str, Any]) -> None:
    from bot import outbox

    interval = max(0.5, float(_cfg().get("poll_interval_s", 2)))
    async with httpx.AsyncClient(timeout=30) as client:
        async def _send(chat_id: Any, text: str) -> None:
            await send_text(row["credentials"], _relay.normalise_phone(str(chat_id)), text, client)

        outbox.register(row["id"], _send)
        try:
            while True:
                try:
                    await poll_once(row, client)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    logger.warning("signal poll failed for %r: %s", row["name"], exc)
                await asyncio.sleep(interval)
        finally:
            outbox.unregister(row["id"])
