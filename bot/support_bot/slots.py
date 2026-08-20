"""Slot extraction: pulling arguments (bot name, mcp server name, backend,
model, config value) out of the free-form text a Support Bot intent was
classified from.

Kept separate from actions.py so the fuzzy-matching logic (stdlib
difflib, no dependency) has one place to live and is easy to test on its
own.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any, Optional

from bot import bot_instances, desktop
from bot.models import KNOWN_MODELS
from bot.router import VALID_BACKENDS

_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")


def _candidates_from_text(text: str) -> list[str]:
    """Words/phrases worth trying as a fuzzy-match target: anything quoted,
    plus every individual word — good enough for short chat-style requests
    like "restart bot X" or 'disable the "filesystem" mcp server'."""
    quoted = _QUOTED_RE.findall(text)
    words = re.findall(r"[A-Za-z0-9_-]+", text)
    return quoted + words


def find_bot_name(text: str) -> Optional[dict[str, Any]]:
    """Fuzzy-matches text against live bot instance names; returns the
    matched instance dict, or None if nothing matched well enough."""
    instances = bot_instances.list_instances()
    if not instances:
        return None
    names = [inst["name"] for inst in instances]
    for candidate in _candidates_from_text(text):
        matches = get_close_matches(candidate, names, n=1, cutoff=0.6)
        if matches:
            return next(inst for inst in instances if inst["name"] == matches[0])
    # Fall back to substring containment (handles multi-word names like
    # "telegram (migrated)" that word-splitting above would never match).
    lowered = text.lower()
    for inst in instances:
        if inst["name"].lower() in lowered:
            return inst
    return None


def find_mcp_server_name(text: str) -> Optional[str]:
    servers = [s["name"] for s in desktop.list_mcp_servers()]
    if not servers:
        return None
    for candidate in _candidates_from_text(text):
        matches = get_close_matches(candidate, servers, n=1, cutoff=0.6)
        if matches:
            return matches[0]
    lowered = text.lower()
    for name in servers:
        if name.lower() in lowered:
            return name
    return None


def find_backend(text: str) -> Optional[str]:
    lowered = text.lower()
    for name in VALID_BACKENDS:
        if name in lowered:
            return name
    return None


def find_model(text: str, backend: Optional[str]) -> Optional[str]:
    quoted = _QUOTED_RE.findall(text)
    if quoted:
        return quoted[0]
    known = KNOWN_MODELS.get(backend or "", [])
    lowered = text.lower()
    for name in known:
        if name.lower() in lowered:
            return name
    # Common shorthand ("opus", "sonnet", "haiku", "fable") for the api
    # backend's closed model list.
    for name in known:
        short = name.split("-")[1] if "-" in name else name
        if short and short in lowered:
            return name
    return None
