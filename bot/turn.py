"""Ephemeral TURN credentials for the Android mesh transport's WebRTC
fallback (see android-app's WebRtcMeshClient.kt) — the standard RFC 5766-
style "shared secret" mechanism coturn's REST API auth (`use-auth-secret`)
implements natively, so BotServer never runs a TURN relay itself or keeps a
TURN user database: it just mints short-lived username/credential pairs
from one secret shared with the actual coturn process, which verifies them
independently using the exact same HMAC. See docs/turn-server-setup.md for
how to stand up the coturn side.

This is deliberately the same shape a hosted TURN provider (Twilio,
Xirsys, etc.) hands back from their own credential-minting endpoint —
{urls, username, credential, ttl} — so a client-side ICE server list needs
no special-casing for "self-hosted" vs. "hosted".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

from bot.config import config

DEFAULT_TTL_S = 3600


def is_enabled() -> bool:
    turn_cfg = config.current.get("turn") or {}
    return bool(turn_cfg.get("enabled") and turn_cfg.get("secret") and turn_cfg.get("urls"))


def credentials(user_label: str = "mesh", ttl_s: Optional[int] = None) -> Optional[dict]:
    """Returns {urls, username, credential, ttl} valid for `ttl_s` seconds,
    or None if TURN isn't configured/enabled — callers treat None as "fall
    back to STUN-only", not an error, since TURN is an optional fallback of
    a fallback."""
    turn_cfg = config.current.get("turn") or {}
    if not turn_cfg.get("enabled"):
        return None
    secret = turn_cfg.get("secret")
    urls = turn_cfg.get("urls")
    if not secret or not urls:
        return None

    ttl_s = max(60, ttl_s if ttl_s is not None else turn_cfg.get("ttl_s", DEFAULT_TTL_S))
    expiry = int(time.time()) + ttl_s
    # coturn's convention: username is "<unix-expiry>:<label>"; the
    # credential is base64(HMAC-SHA1(secret, username)) — coturn recomputes
    # this itself at connect time from the same secret, so nothing here
    # needs to be persisted or looked up later.
    username = f"{expiry}:{user_label}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return {
        "urls": list(urls),
        "username": username,
        "credential": credential,
        "ttl": ttl_s,
    }
