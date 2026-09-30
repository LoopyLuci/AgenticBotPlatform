"""The PrivateProject connectors as ABP modules: one per estate service (LoopyLuci/abp-<service>, each its own private repo),
all running the shared runtime LoopyLuci/abp-private_project-connector over a spec generated from the service's source.

This registers them with the module framework (bot/modules/registry.py) and keeps their sessions current: when you
sign in to the estate, sign out, or start a connector's hub, every running connector gets the private_project-auth session
(auth.set_token). The Router's connector gets the Router's owner token instead, which is what the Router accepts.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bot.private_project.connectors")

GH = "https://github.com/LoopyLuci/"

# The services with a connector: every one with a web surface (bot/private_project/estate.py has the full estate).
CONNECTORS: list[tuple[str, str]] = [
    ("private_project-router", "Router"), ("private_project-auth", "Auth & Hub"), ("private_project-cortex", "Cortex"), ("private_project-ops", "Ops"),
    ("private_project-claude", "Claude wrapper"), ("private_project-ai", "AI"), ("private_project-neith-api", "Neith API"), ("private_project-dash", "Dash"),
    ("private_project-trainer", "Trainer"), ("private_project-budget", "Budget"), ("private_project-health", "Health"), ("private_project-games", "Games"),
    ("private_project-math", "Math"), ("private_project-mma", "MMA"), ("private_project-media", "Media"), ("private_project-edm", "EDM"),
    ("private_project-shopper", "Shopper"), ("private_project-planner", "Planner"), ("private_project-author", "Author"), ("private_project-ee", "EE"),
    ("private_project-science", "Science"), ("private_project-kitchen", "Kitchen"), ("private_project-business", "Business"),
    ("private_project-code", "Code"), ("private_project-tools", "Tools"), ("private_project-blog", "Blog"), ("private_project-tech-site", "Main site"),
    ("private_project-pass", "Pass"), ("private_project-xmpp", "Messenger"),
]
IDS = {sid for sid, _ in CONNECTORS}


def manifests() -> list[dict[str, Any]]:
    """Registry entries (the repos carry their full abp-module.toml; this is enough to find and clone them)."""
    return [{"module": {"id": sid, "name": f"PrivateProject {name}", "repo": f"{GH}abp-{sid}.git", "area": "private_project",
                        "description": f"The PrivateProject {name} service ({sid}) as operations, as the signed-in user."},
             "checkout": {"marker": ["spec.toml", "abp-module.toml"]}}
            for sid, name in CONNECTORS]


def _token_for(mid: str) -> str:
    if mid == "private_project-router":
        from bot.private_project import router
        return router.token()
    from bot.private_project import sso
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
            logger.warning("private_project connector %s: %s", mid, e)
    return out


def on_hub_started(mid: str) -> None:
    if mid in IDS:
        push(mid)
