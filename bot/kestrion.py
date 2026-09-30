"""Kestrion and ABP, each a module of the other: this is ABP's side of the link.

Kestrion (LoopyLuci/Kestrion, a local-first AI development environment) reaches ABP through its own ABP Connector
(its ADR-0096): status, bots, asks, swarms, its MCP server registered here, live events. It uses an ABP integration
key of the "kestrion" preset for that (bot/integrations.py), not ABP's dashboard token.

The other direction is its ADR-0097, implemented here: ABP uses Kestrion as a **backend** ("kestrion",
bot/backends/kestrion_backend.py) through Kestrion's remote session API (`/api/v1/sessions/...`, a paired-device
token). Kestrion's owner grants that from Kestrion's ABP Connector panel: Kestrion mints a device token for ABP and
announces its session API's address here (`POST /api/kestrion/link`), again on every start, because that port is
assigned by the OS each time.

  base_url, default_agent_type, label   config/backends.yaml: backends.kestrion
  the device token                      .env: KESTRION_DEVICE_TOKEN (never returned by any route)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from bot import envfile

logger = logging.getLogger("bot.kestrion")

TOKEN_VAR = "KESTRION_DEVICE_TOKEN"
DEFAULT_AGENT_TYPE = "build"


class KestrionError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _cfg() -> dict:
    from bot.config import config
    return dict(((config.current or {}).get("backends") or {}).get("kestrion") or {})


def base_url() -> str:
    return str(_cfg().get("base_url") or "").rstrip("/")


def token() -> str:
    return (envfile.get_var(TOKEN_VAR) or "").strip()


def default_agent_type() -> str:
    return str(_cfg().get("default_agent_type") or DEFAULT_AGENT_TYPE)


def normalize_url(url: str) -> str:
    u = urlparse((url or "").strip().rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query:
        raise KestrionError("base_url is Kestrion's session API address, like http://127.0.0.1:41231", 400)
    return f"{u.scheme}://{u.netloc}"


def request(method: str, path: str, *, json: Any = None, timeout: float = 20, base: str = "", tok: str = "") -> Any:
    """One call to Kestrion's session API as ABP's paired device."""
    b, t = base or base_url(), tok or token()
    if not b or not t:
        raise KestrionError("Kestrion is not linked: in Kestrion, open Settings -> ABP Connector and let ABP use "
                            "Kestrion as a backend", 409)
    try:
        r = httpx.request(method, b + path, json=json, headers={"Authorization": f"Bearer {t}"}, timeout=timeout)
    except httpx.HTTPError as e:
        raise KestrionError(f"Kestrion does not answer at {b} (is it open? its address changes on each start and it "
                            f"announces the new one itself): {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:300]}
    if r.status_code == 401:
        raise KestrionError("Kestrion refused ABP's device token (revoked?): grant access again from Kestrion's ABP "
                            "Connector", 401)
    if r.status_code >= 400:
        raise KestrionError(str((body or {}).get("error") or f"HTTP {r.status_code}"), r.status_code)
    return body


def link(url: str, device_token: str, *, agent_type: str = "", label: str = "", actor: str = "kestrion") -> dict:
    """Store where Kestrion's session API is and ABP's device token for it, after checking both really work."""
    from bot.config import config
    b = normalize_url(url)
    device_token = (device_token or "").strip() or token()
    if not device_token:
        raise KestrionError("device_token is required the first time", 400)
    request("GET", "/api/v1/sessions", base=b, tok=device_token, timeout=10)      # proves address + token
    if device_token != token():
        envfile.set_var(TOKEN_VAR, device_token, actor=actor)
    cur = _cfg()
    new = {**cur, "base_url": b, "default_agent_type": agent_type or cur.get("default_agent_type") or DEFAULT_AGENT_TYPE,
           "label": label or cur.get("label") or "Kestrion", "linked_at": cur.get("linked_at") or int(time.time()),
           "announced_at": int(time.time())}
    if new != cur:
        config.set_value(["backends", "kestrion"], new, actor=actor)
    return status()


def unlink(actor: str = "dashboard") -> dict:
    from bot.config import config
    envfile.set_var(TOKEN_VAR, "", actor=actor)
    cur = _cfg()
    if cur:
        config.set_value(["backends", "kestrion"], {k: v for k, v in cur.items() if k not in ("base_url",)}, actor=actor)
    return status()


def status() -> dict:
    c = _cfg()
    out: dict[str, Any] = {"linked": bool(base_url() and token()), "base_url": base_url() or None,
                           "default_agent_type": default_agent_type(), "label": c.get("label"),
                           "linked_at": c.get("linked_at"), "announced_at": c.get("announced_at")}
    if out["linked"]:
        try:
            t0 = time.time()
            sessions = request("GET", "/api/v1/sessions", timeout=5)
            out.update(reachable=True, ms=round((time.time() - t0) * 1000),
                       sessions=sessions if isinstance(sessions, list) else sessions)
        except KestrionError as e:
            out.update(reachable=False, error=str(e))
    return out


def ready() -> tuple[bool, str]:
    """For the router's pre-flight check: linked and answering."""
    if not (base_url() and token()):
        return False, ("Kestrion isn't linked - in Kestrion, open Settings -> ABP Connector and let ABP use Kestrion "
                       "as a backend")
    try:
        request("GET", "/api/v1/sessions", timeout=4)
        return True, ""
    except KestrionError as e:
        return False, str(e)


def sessions() -> Any:
    return request("GET", "/api/v1/sessions")


def messages(agent_type: str) -> Any:
    return request("GET", f"/api/v1/sessions/{_seg(agent_type)}/messages")


def models() -> Any:
    return request("GET", "/api/v1/models")


def permissions(agent_type: str) -> Any:
    return request("GET", f"/api/v1/sessions/{_seg(agent_type)}/permissions")


def _seg(s: Optional[str]) -> str:
    s = str(s or "")
    if not s or not all(c.isalnum() or c in "-_." for c in s):
        raise KestrionError("agent_type: letters, digits, - _ . only", 400)
    return s
