"""The Octopus connectors as ABP modules: one per estate service (LoopyLuci/abp-<service>, each its own private repo),
all running the shared runtime LoopyLuci/abp-octopus-connector over a spec generated from the service's source.

This registers them with the module framework (bot/modules/registry.py) and keeps their sessions current: when you
sign in to the estate, sign out, or start a connector's hub, every running connector gets the octopus-auth session
(auth.set_token). The Router's connector gets the Router's owner token instead, which is what the Router accepts.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bot.octopus.connectors")

GH = "https://github.com/LoopyLuci/"

# The services with a connector: every one with a web surface (bot/octopus/estate.py has the full estate).
CONNECTORS: list[tuple[str, str]] = [
    ("octopus-router", "Router"), ("octopus-auth", "Auth & Hub"), ("octopus-cortex", "Cortex"), ("octopus-ops", "Ops"),
    ("octopus-claude", "Claude wrapper"), ("octopus-ai", "AI"), ("octopus-neith-api", "Neith API"), ("octopus-dash", "Dash"),
    ("octopus-trainer", "Trainer"), ("octopus-budget", "Budget"), ("octopus-health", "Health"), ("octopus-games", "Games"),
    ("octopus-math", "Math"), ("octopus-mma", "MMA"), ("octopus-media", "Media"), ("octopus-edm", "EDM"),
    ("octopus-shopper", "Shopper"), ("octopus-planner", "Planner"), ("octopus-author", "Author"), ("octopus-ee", "EE"),
    ("octopus-science", "Science"), ("octopus-kitchen", "Kitchen"), ("octopus-business", "Business"),
    ("octopus-code", "Code"), ("octopus-tools", "Tools"), ("octopus-blog", "Blog"), ("octopus-tech-site", "Main site"),
    ("octopus-pass", "Pass"), ("octopus-xmpp", "Messenger"),
]
IDS = {sid for sid, _ in CONNECTORS}


def manifests() -> list[dict[str, Any]]:
    """Registry entries (the repos carry their full abp-module.toml; this is enough to find and clone them)."""
    return [{"module": {"id": sid, "name": f"Octopus {name}", "repo": f"{GH}abp-{sid}.git", "area": "octopus",
                        "description": f"The Octopus {name} service ({sid}) as operations, as the signed-in user."},
             "checkout": {"marker": ["spec.toml", "abp-module.toml"]}}
            for sid, name in CONNECTORS]


def _token_for(mid: str) -> str:
    if mid == "octopus-router":
        from bot.octopus import router
        return router.token()
    from bot.octopus import sso
    return sso.token()


def push(only: str | None = None) -> dict[str, str]:
    """Give every running connector (or just `only`) its session. Returns {module: outcome}."""
    from bot.modules import client, registry
    out: dict[str, str] = {}
    for mid in sorted(IDS if only is None else {only} & IDS):
        try:
            m = registry.get(mid)
        except Exception:  # noqa: BLE001 - not registered on this install
            continue
        if client.find(m, timeout=1.0) is None:
            continue
        try:
            state = client.call(m, "auth.set_token", {"token": _token_for(mid)}, timeout=15)
            out[mid] = "signed in" if state.get("signed_in") else "signed out"
        except Exception as e:  # noqa: BLE001 - one broken connector never blocks the rest
            out[mid] = f"error: {e}"
            logger.warning("octopus connector %s: %s", mid, e)
    return out


def on_hub_started(mid: str) -> None:
    if mid in IDS:
        push(mid)
