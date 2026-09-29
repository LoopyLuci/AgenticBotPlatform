"""ABP's client for octopus-router (Octopus-Security/octopus-router): its status, models, usage, conversations,
chat, agent runs, workspaces and missions, with the Router's owner token.

The token lives in ABP's .env as OCTOPUS_ROUTER_TOKEN (set from the Octopus page; never returned by any API),
and the Router's address is `octopus.router_url` (default http://127.0.0.1:3030, the Router's own default).
The Router also serves an OpenAI-compatible `/v1` (metered on your own keys), so while it is configured it is
the provider "octopus-router" (bot/providers.py) and its routing aliases (auto, local-big, ...) are models.

Only the calls below exist: an allowlist, the same stance the Router takes toward ABP (its botplatform.js).
Router keys come back as presence and fingerprints only; the Router never returns key values.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from bot import envfile

DEFAULT_URL = "http://127.0.0.1:3030"
TOKEN_VAR = "OCTOPUS_ROUTER_TOKEN"
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class RouterError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def url() -> str:
    try:
        from bot.config import config
        u = ((config.current or {}).get("octopus") or {}).get("router_url")
    except Exception:  # noqa: BLE001
        u = None
    return str(u or DEFAULT_URL).rstrip("/")


def token() -> str:
    return (envfile.get_var(TOKEN_VAR) or "").strip()


def configured() -> bool:
    return bool(token())


def set_token(value: str) -> None:
    value = (value or "").strip()
    if value and len(value) < 16:
        raise ValueError("that is too short to be the Router's owner token")
    envfile.set_var(TOKEN_VAR, value, actor="dashboard")


def _check(ident: str, what: str) -> str:
    if not _ID.match(str(ident)):
        raise RouterError(f"bad {what}", 400)
    return str(ident)


def request(method: str, path: str, body: Any = None, *, auth: bool = True, timeout: float = 60.0,
            params: Optional[dict] = None) -> Any:
    headers = {"User-Agent": "AgenticBotPlatform/octopus"}
    if auth:
        t = token()
        if not t:
            raise RouterError("no Router owner token yet: add it on the Octopus page (Router tab)", 412)
        headers["Authorization"] = f"Bearer {t}"
    try:
        r = httpx.request(method, url() + path, json=body, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise RouterError(f"cannot reach the Router at {url()}: {e}", 502) from e
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = {"text": r.text[:2000]}
    if r.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else None
        raise RouterError(f"Router: {msg or f'HTTP {r.status_code}'}", r.status_code)
    return data


# ---- the allowlist -------------------------------------------------------------------------------------------

def status() -> dict:
    out: dict[str, Any] = {"url": url(), "configured": configured()}
    try:
        out["build"] = request("GET", "/api/build", auth=False, timeout=5)
        out["reachable"] = True
    except RouterError as e:
        out.update(reachable=False, error=str(e))
        return out
    if configured():
        try:
            me = request("GET", "/api/me", timeout=10)
            out["owner"] = me.get("owner")
            out["providers"] = [p.get("id") for p in me.get("providers") or []]
            out["authorized"] = True
        except RouterError as e:
            out.update(authorized=False, error=str(e))
    return out


def models() -> Any:
    return request("GET", "/api/models")


def usage() -> Any:
    return request("GET", "/api/usage")


def keys() -> Any:
    return request("GET", "/api/keys")


def conversations() -> Any:
    return request("GET", "/api/conversations")


def messages(conversation_id: str) -> Any:
    return request("GET", f"/api/conversations/{_check(conversation_id, 'conversation id')}/messages")


def chat(messages_: list[dict], model: str = "auto", system: Optional[str] = None, max_cost: Optional[str] = None) -> Any:
    if not messages_:
        raise RouterError("messages required", 400)
    body: dict[str, Any] = {"messages": messages_, "model": model or "auto"}
    if system:
        body["system"] = system
    if max_cost:
        body["maxCost"] = max_cost
    return request("POST", "/api/chat", body, timeout=600)


def route_preview(messages_: list[dict], model: str = "auto") -> Any:
    return request("POST", "/api/route/preview", {"messages": messages_, "model": model})


def runs() -> Any:
    return request("GET", "/api/runs")


def start_run(body: dict) -> Any:
    return request("POST", "/api/runs", body, timeout=60)


def confirm_run(run_id: str, answer: dict) -> Any:
    return request("POST", f"/api/runs/{_check(run_id, 'run id')}/confirm", answer)


def cancel_run(run_id: str) -> Any:
    return request("POST", f"/api/runs/{_check(run_id, 'run id')}/cancel", {})


def workspaces() -> Any:
    return request("GET", "/api/workspaces")


def missions() -> Any:
    return request("GET", "/api/missions")


def mission(mission_id: str) -> Any:
    return request("GET", f"/api/missions/{_check(mission_id, 'mission id')}")


def settings() -> Any:
    return request("GET", "/api/settings")


def botplatform_status() -> Any:
    """What the Router sees of ABP through its Bot Platform view (tests the integration key end to end)."""
    return request("GET", "/api/botplatform/status")


def openai_provider() -> Optional[dict]:
    """The Router's /v1 as a provider entry, when a token is set."""
    t = token()
    if not t:
        return None
    return {"base_url": url() + "/v1", "protocol": "openai", "api_key": t, "module": "octopus-router",
            "description": "octopus-router: its routing aliases and every provider you hold a key for there"}
