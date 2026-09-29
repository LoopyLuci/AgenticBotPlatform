"""The PrivateProject estate (PrivateProject-Security): every service, where it answers, and whether it is up.

The catalog comes from the estate's own records (private_project-vault/ARCHITECTURE.md: repos, subdomains; each repo's
compose file and routes) as of 2026-09-29. Four services do not answer on a subdomain named after their repo
(plan, shop, write, chat); the table says where each really is. Anything here can be overridden in
config/backends.yaml under `private_project.services.<id>` (url, subdomain, disabled), and `private_project.domain` sets the base
domain.

Status is each service's `/api/build` (or its own health path), fetched in parallel and cached for a minute. A
service with no web surface (a library, a flake, a bot) has no probe and is shown as such.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

DEFAULT_DOMAIN = "private_projecttechnology.net"
REPO_ORG = "PrivateProject-Security"

# id, name, kind, subdomain (None = no public web surface), health path, one-line purpose
_CATALOG: list[tuple[str, str, str, Optional[str], str, str]] = [
    # control plane
    ("private_project-router", "Router", "platform", None, "/api/build",
     "BYOK multi-model coding assistant: chat, agentic tool loop over workspaces, crews, missions, Bot Platform view"),
    ("private_project-auth", "Auth & Hub", "platform", "auth", "/api/build",
     "The only token issuer (RS256 JWT, TOTP 2FA) and the installable app hub at its root"),
    ("private_project-cortex", "Cortex", "platform", "chat", "/api/build",
     "Multi-agent AI workspace, Discord/Telegram bots, admin panel"),
    ("private_project-ops", "Ops", "platform", None, "/api/build",
     "Privileged operations plane: deterministic git + redeploy + host status, reachable only by cortex"),
    ("private_project-claude", "Claude wrapper", "platform", None, "/api/build", "HTTP wrapper around the Claude Code CLI"),
    ("private_project-ai", "AI", "platform", None, "/api/build", "Local-inference orchestration and network diagnostics"),
    ("private_project-mcp", "MCP", "platform", None, "", "MCP server over the Docker socket (read-only mount)"),
    ("private_project-neith-api", "Neith API", "platform", None, "/api/build", "Server-management tool API"),
    ("private_project-auth-client", "Auth client", "library", None, "", "JWT client + Express middleware (@private_project-security/auth-client)"),
    # applications
    ("private_project-budget", "Budget", "app", "budget", "/api/build", "Budget and subscription tracker"),
    ("private_project-health", "Health", "app", "health", "/api/build", "Health and fitness tracker"),
    ("private_project-games", "Games", "app", "games", "/api/build", "Games platform and game-server management"),
    ("private_project-math", "Math", "app", "math", "/api/build", "Math practice and quizzes"),
    ("private_project-mma", "MMA", "app", "mma", "", "MMA training content"),
    ("private_project-media", "Media", "app", "media", "/api/build", "Media app"),
    ("private_project-edm", "EDM", "app", "edm", "/api/build", "Music production with ML features"),
    ("private_project-shopper", "Shopper", "app", "shop", "/api/build", "Price comparison and shopping"),
    ("private_project-planner", "Planner", "app", "plan", "/api/build", "Linked-notes brain on an infinite canvas"),
    ("private_project-author", "Author", "app", "write", "/api/build", "Writing"),
    ("private_project-ee", "EE", "app", "ee", "/api/build", "Breadboard planner, parts catalogue, design review, firmware"),
    ("private_project-science", "Science", "app", "science", "/api/build", "Science app"),
    ("private_project-kitchen", "Kitchen", "app", "kitchen", "/api/build", "Kitchen and recipes"),
    ("private_project-business", "Business", "app", "business", "/healthz", "Business and tax"),
    ("private_project-code", "Code", "app", "code", "", "Code learning site"),
    ("private_project-tools", "Tools", "app", "tools", "", "Static tools (speed reader and more)"),
    ("private_project-dash", "Dash", "app", None, "/api/build", "Per-device system dashboard"),
    ("private_project-trainer", "Trainer", "app", None, "/api/build", "Model training service (Python)"),
    ("private_project-blog", "Blog", "app", "blog", "", "Blog"),
    ("private_project-tech-site", "Main site", "app", "", "", "private_projecttechnology.net and the apps landing page"),
    ("alfred-js", "Alfred", "bot", None, "", "Discord bot"),
    # data, security, infrastructure
    ("private_project-pass", "Pass", "infra", "pass", "", "Password manager (Vaultwarden)"),
    ("private_project-vault", "Vault", "infra", None, "", "Syncthing vault, agent memory, the estate's records"),
    ("private_project-proxy-manager", "Proxy manager", "infra", None, "", "Reverse proxy, ports 80/443, Let's Encrypt"),
    ("private_project-xmpp", "Messenger", "infra", "xmpp", "", "XMPP chat (Prosody, Converse.js, coturn)"),
    ("private_project-simplex", "SimpleX", "infra", None, "", "SimpleX relay servers"),
    ("private_project-mail", "Mail", "infra", "mail", "", "Mailcow (currently down)"),
    ("nixos-hetzner", "Server (NixOS)", "infra", None, "", "The Hetzner server as a NixOS flake: disks, firewall, Tailscale"),
    ("private_project-conversation-exporter", "Conversation exporter", "tool", None, "", "Conversation export utility"),
    # security training
    ("Cephaloscan", "Cephaloscan", "security", None, "", "Security scanning workbench and labs"),
    ("PentestPlayground", "Pentest Playground", "security", None, "", "Pentest courses, exercises and cheatsheets"),
    ("pentest-flake", "Pentest flake", "security", None, "", "NixOS pentest machines"),
]

_cache: dict[str, Any] = {"at": 0.0, "status": {}}
_TTL_S = 60.0


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("private_project") or {})
    except Exception:  # noqa: BLE001
        return {}


def domain() -> str:
    return str(_cfg().get("domain") or DEFAULT_DOMAIN).strip().strip(".")


def services() -> list[dict]:
    overrides = _cfg().get("services") or {}
    d = domain()
    out = []
    for sid, name, kind, sub, health, purpose in _CATALOG:
        o = overrides.get(sid) or {}
        if o.get("disabled"):
            continue
        sub = o.get("subdomain", sub)
        url = o.get("url") or (None if sub is None else (f"https://{sub}.{d}" if sub else f"https://{d}"))
        out.append({"id": sid, "name": name, "kind": kind, "url": url, "health": o.get("health", health),
                    "purpose": purpose, "repo": f"https://github.com/{REPO_ORG}/{sid}"})
    return out


def service(sid: str) -> Optional[dict]:
    return next((s for s in services() if s["id"] == sid), None)


async def _probe(client: httpx.AsyncClient, s: dict) -> tuple[str, dict]:
    if not s["url"] or not s["health"]:
        return s["id"], {"state": "no-probe"}
    t0 = time.monotonic()
    try:
        r = await client.get(s["url"].rstrip("/") + s["health"])
        ms = round((time.monotonic() - t0) * 1000)
        body: Any = None
        if "json" in r.headers.get("content-type", ""):
            try:
                body = r.json()
            except ValueError:
                body = None
        build = body if isinstance(body, dict) else {}
        state = "up" if r.status_code < 400 else ("auth" if r.status_code in (401, 403) else "down")
        return s["id"], {"state": state, "http": r.status_code, "ms": ms,
                         "build": {k: build[k] for k in ("service", "version", "commit", "sha", "startedAt", "builtAt")
                                   if k in build}}
    except httpx.HTTPError as e:
        return s["id"], {"state": "down", "error": type(e).__name__, "ms": round((time.monotonic() - t0) * 1000)}


async def status(refresh: bool = False) -> dict[str, dict]:
    if not refresh and time.monotonic() - _cache["at"] < _TTL_S and _cache["status"]:
        return dict(_cache["status"])
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=False,
                                 headers={"User-Agent": "AgenticBotPlatform-private_project/1"}) as client:
        pairs = await asyncio.gather(*(_probe(client, s) for s in services()))
    _cache.update(at=time.monotonic(), status=dict(pairs))
    return dict(_cache["status"])
