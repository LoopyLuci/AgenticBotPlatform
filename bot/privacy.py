"""Privacy mode: one switch that keeps every model call and every agent tool on this machine.

When it is on:
  - the router (bot/router.py) only uses backends whose model runs here: a provider or Kestrion at a loopback
    address (ABP's own model server, Ollama, LM Studio, llama.cpp, vLLM on localhost). Claude (API, CLI, Desktop),
    Hermes, OpenCode and OpenClaw talk to cloud models and are skipped; a turn with no local backend in its chain fails
    with a clear message instead of quietly going to the cloud.
  - agent tools that reach the network are refused (web search and fetch, the browser, batch and multi-model
    consultation through cloud providers, external MCP servers that are not local).
  - embeddings for the shared memory come only from a local runtime (bot/memoryfabric falls back to hashed TF-IDF).
  allow_lan also counts private-network addresses (another machine of yours) as local.

Settings live in <data>/privacy.json; the dashboard and `abp privacy` change them. A command an agent runs in the
shell can still reach the network: privacy mode governs ABP's own calls, not arbitrary programs (use the sandbox's
network=none for that).
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

CLOUD_BACKENDS = frozenset({"api", "cli", "ui", "hermes_cli", "hermes_gateway", "opencode", "openclaw"})
NETWORK_TOOLS = frozenset({"web_search", "web_fetch", "browser_navigate", "browser_click", "browser_type", "browser_snapshot",
                           "browser_screenshot", "browser", "consult_models", "dispatch_batch_completions",
                           "check_batch_status", "get_batch_results", "fetch_url", "http_request"})
_cache: dict[str, Any] = {}


def _path() -> Path:
    env = os.environ.get("ABP_PRIVACY_FILE", "").strip()
    if env:
        return Path(env)
    from bot.envfile import PROJECT_ROOT
    return PROJECT_ROOT / "data" / "privacy.json"


def settings() -> dict:
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"enabled": False, "allow_lan": False}
    hit = _cache.get("s")
    if hit and hit[0] == (str(p), mtime):
        return hit[1]
    try:
        st = {"enabled": False, "allow_lan": False, **json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        st = {"enabled": True, "allow_lan": False}           # an unreadable file fails closed
    _cache["s"] = ((str(p), mtime), st)
    return st


def set_settings(enabled: Optional[bool] = None, allow_lan: Optional[bool] = None) -> dict:
    st = dict(settings())
    if enabled is not None:
        st["enabled"] = bool(enabled)
    if allow_lan is not None:
        st["allow_lan"] = bool(allow_lan)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    _cache.clear()
    return st


def enabled() -> bool:
    return bool(settings()["enabled"])


def is_local_url(url: str, allow_lan: Optional[bool] = None) -> bool:
    """A model or service address that stays on this machine (or, with allow_lan, on the private network)."""
    lan = settings()["allow_lan"] if allow_lan is None else allow_lan
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host:
        return False
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return False
    return bool(addrs) and all(a.is_loopback or (lan and (a.is_private or a.is_link_local)) for a in addrs)


def backend_endpoint(backend: Any) -> str:
    return str(getattr(backend, "base_url", "") or getattr(getattr(backend, "transport", None), "base_url", "") or "")


def backend_is_local(name: str, backend: Any = None) -> bool:
    if name in CLOUD_BACKENDS:
        return False
    url = backend_endpoint(backend) if backend is not None else ""
    return bool(url) and is_local_url(url)


def mcp_url_for_tool(name: str) -> str:
    """The address of the external MCP server serving `name` ("" for ABP's own tools and local stdio servers)."""
    try:
        from bot import db
        from bot.agent_runtime import mcp_client
        server = mcp_client.server_for_tool(name)
        if not server:
            return ""
        row = db.get_external_mcp_server(server)
        return str(row["url"] or "") if row is not None and (row["transport"] if "transport" in row.keys() else "") != "stdio" else ""
    except Exception:  # noqa: BLE001 - unknown: treated as ABP's own tool
        return ""


def check_tool(name: str, server_url: str = "") -> Optional[str]:
    """Why a tool call is refused in privacy mode, or None."""
    if not enabled():
        return None
    if name in NETWORK_TOOLS or name.startswith(("web_", "browser_")):
        return f"privacy mode is on: {name} reaches the network (turn privacy mode off to use it)"
    if server_url and not is_local_url(server_url):
        return f"privacy mode is on: {name} is served by {urlparse(server_url).hostname}, not this machine"
    return None
