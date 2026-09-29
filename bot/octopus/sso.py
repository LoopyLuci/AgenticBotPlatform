"""Signing ABP in to the Octopus estate: octopus-auth, the estate's only token issuer (RS256 JWT, TOTP 2FA).

The owner types their username, password and authenticator (or recovery) code on ABP's Octopus page; ABP sends
them once to `https://auth.<domain>/api/auth/login` and keeps only the session token it gets back, in .env as
OCTOPUS_SSO_TOKEN. The password and code are never stored. The token (7 days) is refreshed through
`/api/auth/refresh` before it expires, and checked with `/api/auth/verify`, which honours revocation. The token
is what ABP's Octopus connectors send (Authorization: Bearer) to the estate's apps; no API returns it.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

import httpx

from bot import envfile
from bot.octopus import estate

TOKEN_VAR = "OCTOPUS_SSO_TOKEN"


class SsoError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def auth_url() -> str:
    s = estate.service("octopus-auth")
    return (s["url"] if s and s["url"] else f"https://auth.{estate.domain()}").rstrip("/")


def token() -> str:
    return (envfile.get_var(TOKEN_VAR) or "").strip()


def claims(tok: Optional[str] = None) -> dict:
    """The token's payload (not verified here; auth verifies). Used for expiry and the username."""
    tok = tok if tok is not None else token()
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _post(path: str, body: Optional[dict] = None, bearer: Optional[str] = None) -> tuple[int, dict]:
    headers = {"User-Agent": "AgenticBotPlatform/octopus"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        r = httpx.post(auth_url() + path, json=body or {}, headers=headers, timeout=20)
    except httpx.HTTPError as e:
        raise SsoError(f"cannot reach {auth_url()}: {e}", 502) from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


def login(username: str, password: str, code: str = "") -> dict:
    if not username or not password:
        raise SsoError("username and password are required")
    status, data = _post("/api/auth/login", {"username": username, "password": password, "totpCode": code or ""})
    if status >= 400 or not data.get("success"):
        raise SsoError(data.get("error") or f"sign-in failed (HTTP {status})", status if status >= 400 else 401)
    tok = data.get("token")
    if not tok:
        # enrolment or challenge responses carry no session token: 2FA must be set up on auth's own page first
        raise SsoError("this account must finish setting up 2FA at " + auth_url() + "/login first", 409)
    envfile.set_var(TOKEN_VAR, tok, actor="dashboard")
    return state()


def logout() -> dict:
    envfile.set_var(TOKEN_VAR, "", actor="dashboard")
    return state()


def verify() -> dict:
    tok = token()
    if not tok:
        return {"valid": False}
    status, data = _post("/api/auth/verify", bearer=tok)
    return {"valid": status == 200 and bool(data.get("valid")), "user": data.get("user"), "http": status}


def refresh_if_needed(min_left_s: int = 2 * 86400) -> bool:
    """Swap the token for a fresh one when under two days are left. True if refreshed."""
    tok = token()
    exp = claims(tok).get("exp")
    if not tok or not isinstance(exp, (int, float)) or exp - time.time() > min_left_s:
        return False
    status, data = _post("/api/auth/refresh", bearer=tok)
    if status == 200 and data.get("token"):
        envfile.set_var(TOKEN_VAR, data["token"], actor="octopus-sso")
        return True
    return False


def state() -> dict[str, Any]:
    c = claims()
    exp = c.get("exp")
    return {"signed_in": bool(token()), "auth_url": auth_url(), "username": c.get("username") or c.get("sub"),
            "role": c.get("role"), "expires_at": exp, "expires_in_s": int(exp - time.time()) if isinstance(exp, (int, float)) else None}
