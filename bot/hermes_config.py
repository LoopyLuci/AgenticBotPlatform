"""Reads/writes the `delegation:` section of Hermes Agent's own
~/.hermes/config.yaml — a third-party file BotServer does not own.

This is deliberately narrow: only the `delegation` mapping is ever
touched, and only the keys explicitly passed to set_delegation() are
changed — every other key/section, comment, and formatting choice in the
user's real config is preserved byte-for-byte via ruamel.yaml's
round-trip loader (a plain yaml.safe_load()/safe_dump() round trip would
silently strip every one of Hermes's own extensive inline comments, which
this project's own cli-config.yaml.example shows are load-bearing
documentation, not decoration).

Until Phase 5 of the Hermes-swarm plan (per-instance HERMES_HOME) lands,
every hermes_gateway-backed bot instance on this machine shares this one
file — a change here affects every one of them, not just the instance
`configure_delegation()` was called for. This is an honestly-documented
limitation, not silently pretended away (see bot/mcp_server.py's
configure_delegation docstring).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot.hermes_config")

HERMES_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"

_DELEGATION_KEYS = (
    "provider",
    "model",
    "max_concurrent_children",
    "max_spawn_depth",
    "subagent_auto_approve",
)


def _yaml():
    from ruamel.yaml import YAML

    y = YAML(typ="rt")  # round-trip: preserves comments, key order, quoting style
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def read_delegation_config() -> dict[str, Any]:
    """The current `delegation:` section, or {} if the file or section
    doesn't exist yet (Hermes has never been run, or delegation has never
    been configured — both mean "everything at Hermes's own defaults")."""
    if not HERMES_CONFIG_PATH.is_file():
        return {}
    try:
        yaml = _yaml()
        with HERMES_CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.load(f) or {}
        return dict(data.get("delegation") or {})
    except Exception as exc:
        logger.warning("read_delegation_config: failed to read %s: %s", HERMES_CONFIG_PATH, exc)
        return {}


def set_delegation_config(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_concurrent_children: Optional[int] = None,
    max_spawn_depth: Optional[int] = None,
    subagent_auto_approve: Optional[bool] = None,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Merges only the given (non-None) keys into Hermes's own
    `delegation:` section and writes the file back atomically
    (tmp-then-replace), preserving everything else in the file exactly as
    it was. Returns the resulting delegation dict. Creates
    ~/.hermes/config.yaml fresh (containing only this one section) if it
    doesn't exist yet — Hermes merges a partial config over its own
    built-in defaults, the same as a user hand-seeding
    cli-config.yaml.example's documented shape."""
    from bot import db

    changes = {
        k: v
        for k, v in {
            "provider": provider,
            "model": model,
            "max_concurrent_children": max_concurrent_children,
            "max_spawn_depth": max_spawn_depth,
            "subagent_auto_approve": subagent_auto_approve,
        }.items()
        if v is not None
    }
    if not changes:
        return read_delegation_config()

    yaml = _yaml()
    if HERMES_CONFIG_PATH.is_file():
        with HERMES_CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.load(f) or {}
    else:
        data = {}
        HERMES_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    delegation = data.get("delegation")
    if delegation is None:
        delegation = {}
        data["delegation"] = delegation
    delegation.update(changes)

    tmp_path = HERMES_CONFIG_PATH.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    tmp_path.replace(HERMES_CONFIG_PATH)  # atomic on the same filesystem

    db.log_audit(actor=actor, action="hermes_delegation_config", detail=str(changes))
    logger.info("delegation config updated: %s", changes)
    return dict(delegation)
