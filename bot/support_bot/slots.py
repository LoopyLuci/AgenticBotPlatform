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

from bot import bot_instances, db, desktop, envfile
from bot.models import KNOWN_MODELS
from bot.router import VALID_BACKENDS

_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_NUMBER_RE = re.compile(r"#?(\d+)")


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


def find_quoted(text: str) -> Optional[str]:
    """First quoted substring, if any — used for free-text slots like a
    device label or a session search term."""
    quoted = _QUOTED_RE.findall(text)
    return quoted[0] if quoted else None


def find_number(text: str) -> Optional[int]:
    """First bare or #-prefixed integer in the text — job ids, session
    ids, etc. ("check job #17", "show me session 5")."""
    m = _NUMBER_RE.search(text)
    return int(m.group(1)) if m else None


def find_swarm(text: str) -> Optional[dict[str, Any]]:
    """Fuzzy-matches text against configured swarm names."""
    swarms = [dict(r) for r in db.list_swarms()]
    if not swarms:
        return None
    names = [s["name"] for s in swarms]
    for candidate in _candidates_from_text(text):
        matches = get_close_matches(candidate, names, n=1, cutoff=0.6)
        if matches:
            return next(s for s in swarms if s["name"] == matches[0])
    lowered = text.lower()
    for s in swarms:
        if s["name"].lower() in lowered:
            return s
    return None


def find_swarm_run_id(text: str) -> Optional[str]:
    """A swarm_run_id is a uuid4 hex string — not something anyone types
    from memory, so this only catches it if it's literally pasted in
    (e.g. quoted, or a bare 8+ char hex token)."""
    quoted = _QUOTED_RE.findall(text)
    if quoted:
        return quoted[0]
    for word in re.findall(r"[0-9a-fA-F]{8,32}", text):
        return word
    return None


def extract_swarm_prompt(text: str) -> Optional[str]:
    """Pulls the part after "with prompt:"/"prompt:"/"saying" out of a
    swarm_run request, e.g. 'run swarm X with prompt: summarize this'."""
    m = re.search(r"(?:with\s+prompt|prompt|saying)\s*[:\-]?\s*(.+)$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip(" \"'")
    return None


# Maps a spoken/typed phrase fragment to a (config path, kind) pair for
# settings_show/settings_set. "kind" is "bool" or "mode" (a fixed set of
# string values, e.g. agent_control.mode).
SETTINGS: dict[str, tuple[list[str], str]] = {
    "ui automation": (["features", "ui_automation_enabled"], "bool"),
    "confirm destructive": (["security", "confirm_destructive"], "bool"),
    "verbose telemetry": (["features", "verbose_telemetry"], "bool"),
    "agent control": (["agent_control", "mode"], "mode"),
}


def find_setting(text: str) -> Optional[tuple[list[str], str]]:
    lowered = text.lower()
    for phrase, entry in SETTINGS.items():
        if phrase in lowered:
            return entry
    return None


def find_bool(text: str) -> Optional[bool]:
    lowered = text.lower()
    if any(w in lowered for w in ("enable", "turn on", "on", "true", "yes", "activate")):
        return True
    if any(w in lowered for w in ("disable", "turn off", "off", "false", "no", "deactivate")):
        return False
    return None


def find_agent_control_mode(text: str) -> Optional[str]:
    lowered = text.lower()
    if "allowlist" in lowered:
        return "allowlist"
    if "trust" in lowered:
        return "trust_all"
    return None


def find_device(text: str) -> Optional[dict[str, Any]]:
    """Fuzzy-matches text against paired device labels."""
    devices = [dict(r) for r in db.list_devices()]
    if not devices:
        return None
    labels = [d["label"] for d in devices]
    for candidate in _candidates_from_text(text):
        matches = get_close_matches(candidate, labels, n=1, cutoff=0.6)
        if matches:
            return next(d for d in devices if d["label"] == matches[0])
    lowered = text.lower()
    for d in devices:
        if d["label"].lower() in lowered:
            return d
    return None


def find_backup_name(text: str) -> Optional[str]:
    """Fuzzy-matches text against both .env and bot-instance backup
    filenames — the two systems' names are prefixed distinctly ("env-" vs
    "instances-") so a match unambiguously tells the caller which system
    to restore from."""
    names = [b["name"] for b in envfile.list_backups()] + [b["name"] for b in bot_instances.list_backups()]
    if not names:
        return None
    for candidate in _candidates_from_text(text):
        matches = get_close_matches(candidate, names, n=1, cutoff=0.5)
        if matches:
            return matches[0]
    return None
