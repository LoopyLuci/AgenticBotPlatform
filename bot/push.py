"""Push notifications to the Android app via Firebase Cloud Messaging's
HTTP v1 API, for new inbound bot messages arriving while the app isn't
actively polling.

Entirely optional — if no service account is configured (FCM_SERVICE_ACCOUNT_JSON
unset, or the file's missing/invalid), notify_new_message() is a fast no-op.
Every call site fires this via asyncio.create_task and never awaits or
propagates its errors — a push-delivery problem must never slow down or
break real message handling, which is why this stays out of bot/db.py
(a pure persistence layer) entirely.

Uses the standard Google service-account OAuth2 JWT-bearer flow, hand-
rolled with PyJWT + cryptography rather than pulling in google-auth — both
of those are already transitive dependencies of this project (discord.py/
slack_bolt), so this needed no heavier new dependency, matching the
qrcode/pypng-over-Pillow precedent elsewhere in this codebase.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx
import jwt

from bot import db, envfile

logger = logging.getLogger("bot.push")

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_cached_access_token: Optional[str] = None
_cached_expiry: float = 0.0


def _service_account() -> Optional[dict[str, Any]]:
    path = os.environ.get("FCM_SERVICE_ACCOUNT_JSON") or envfile.get_var("FCM_SERVICE_ACCOUNT_JSON")
    if not path:
        return None
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception as exc:
        logger.warning("FCM_SERVICE_ACCOUNT_JSON set but unreadable: %s", exc)
        return None


async def _access_token(account: dict[str, Any]) -> Optional[str]:
    global _cached_access_token, _cached_expiry
    if _cached_access_token and time.time() < _cached_expiry - 60:
        return _cached_access_token

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": account["client_email"],
            "scope": _SCOPE,
            "aud": _TOKEN_URI,
            "iat": now,
            "exp": now + 3600,
        },
        account["private_key"],
        algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_URI,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    if resp.status_code != 200:
        logger.warning("FCM token exchange failed: %s %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    _cached_access_token = data["access_token"]
    _cached_expiry = time.time() + data.get("expires_in", 3600)
    return _cached_access_token


async def notify_new_message(instance_name: str, text: str) -> None:
    """Fire-and-forget — call via asyncio.create_task(...), never awaited
    for its result. Swallows all errors internally; logs, doesn't raise."""
    try:
        account = _service_account()
        if account is None:
            return
        tokens = [row["fcm_token"] for row in db.list_push_tokens()]
        if not tokens:
            return
        access_token = await _access_token(account)
        if access_token is None:
            return
        project_id = account["project_id"]
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        preview = text.strip().replace("\n", " ")[:120] or "New message"
        async with httpx.AsyncClient(timeout=10.0) as client:
            for token in tokens:
                payload = {
                    "message": {
                        "token": token,
                        "notification": {"title": instance_name, "body": preview},
                        "data": {"instance_name": instance_name},
                    }
                }
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 404 or (resp.status_code == 400 and "UNREGISTERED" in resp.text):
                        # Stale token — the device uninstalled or re-paired
                        # with a fresh FCM registration; drop it rather than
                        # retrying it forever.
                        conn = db.get_conn()
                        with db._lock:
                            conn.execute("DELETE FROM push_tokens WHERE fcm_token=?", (token,))
                            conn.commit()
                except Exception as exc:
                    logger.warning("FCM send failed for one device: %s", exc)
    except Exception as exc:
        logger.warning("notify_new_message failed: %s", exc)
