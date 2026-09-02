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
import sys
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


def _config_path(hermes_home: Optional[str] = None) -> Path:
    """`hermes_home`/config.yaml when given (a Phase-5-isolated instance's
    own Hermes home — matches Hermes's own HERMES_HOME resolution order,
    explicit override first), else the shared machine-wide default every
    non-isolated hermes_gateway instance still uses."""
    if hermes_home:
        return Path(hermes_home).expanduser() / "config.yaml"
    return HERMES_CONFIG_PATH


def _load_yaml_or_empty(path: Path, yaml) -> dict[str, Any]:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.load(f) or {}


def _atomic_write_yaml(path: Path, data: dict, yaml) -> None:
    tmp_path = path.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    tmp_path.replace(path)  # atomic on the same filesystem


def read_delegation_config(hermes_home: Optional[str] = None) -> dict[str, Any]:
    """The current `delegation:` section, or {} if the file or section
    doesn't exist yet (Hermes has never been run, or delegation has never
    been configured — both mean "everything at Hermes's own defaults")."""
    path = _config_path(hermes_home)
    if not path.is_file():
        return {}
    try:
        yaml = _yaml()
        with path.open(encoding="utf-8") as f:
            data = yaml.load(f) or {}
        return dict(data.get("delegation") or {})
    except Exception as exc:
        logger.warning("read_delegation_config: failed to read %s: %s", path, exc)
        return {}


def set_delegation_config(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_concurrent_children: Optional[int] = None,
    max_spawn_depth: Optional[int] = None,
    subagent_auto_approve: Optional[bool] = None,
    hermes_home: Optional[str] = None,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Merges only the given (non-None) keys into Hermes's own
    `delegation:` section and writes the file back atomically
    (tmp-then-replace), preserving everything else in the file exactly as
    it was. Returns the resulting delegation dict. Creates the config file
    fresh (containing only this one section) if it doesn't exist yet —
    Hermes merges a partial config over its own built-in defaults, the
    same as a user hand-seeding cli-config.yaml.example's documented
    shape. Pass `hermes_home` for a Phase-5-isolated instance to target
    its own config.yaml instead of the shared default."""
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
        return read_delegation_config(hermes_home)

    path = _config_path(hermes_home)
    yaml = _yaml()
    data = _load_yaml_or_empty(path, yaml)

    delegation = data.get("delegation")
    if delegation is None:
        delegation = {}
        data["delegation"] = delegation
    delegation.update(changes)

    _atomic_write_yaml(path, data, yaml)

    db.log_audit(actor=actor, action="hermes_delegation_config", detail=f"{path}: {changes}")
    logger.info("delegation config updated at %s: %s", path, changes)
    return dict(delegation)


def register_botserver_mcp_server(
    *,
    hermes_home: Optional[str] = None,
    dashboard_token: Optional[str] = None,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Registers BotServer's own MCP control server (bot/mcp_server.py)
    into this Hermes instance's `mcp_servers:` section under the name
    "botserver", pointing at the same interpreter this BotServer process
    is running under — mirrors bot/desktop.py's register_self_mcp() for
    Claude Desktop, adapted to Hermes's `mcp_servers:` (snake_case) config
    key rather than Claude Desktop's `mcpServers` (camelCase)
    claude_desktop_config.json.

    This is what completes the "Hermes can organize swarms too" half of
    the Hermes-swarm plan: a Hermes agent already gets its own delegate_task
    for spawning its own children (Phase 3), but had no way to reach
    OTHER BotServer bot_instances (a different Hermes worker, or a
    Claude-backed one) the way Claude itself can via this same MCP
    server, or an `api`-backend agent can via delegate_to_instance
    (Phase 4). Registering this server gives a Hermes agent — mid-turn,
    via its own native MCP client, the exact mechanism real installs
    already use for the "agentic_toolkit" entry — direct access to
    ask_instance/run_swarm/dispatch_swarm_goal/list_available_models/
    create_bot_instance/configure_delegation, closing the loop in both
    directions.

    Idempotent — re-running overwrites the one "botserver" entry (so it
    stays correct after a token rotation). Takes effect only once this
    instance's Hermes gateway process is restarted — `mcp_servers` are
    read at gateway startup, not hot-reloaded — so the caller is expected
    to evict/reconnect the backend afterward (see the dashboard route
    this backs, which does exactly that)."""
    from bot import db

    python = sys.executable
    project_root = str(_project_root())
    env_vars = {"PYTHONPATH": project_root}
    if dashboard_token:
        env_vars["DASHBOARD_TOKEN"] = dashboard_token

    path = _config_path(hermes_home)
    yaml = _yaml()
    data = _load_yaml_or_empty(path, yaml)

    mcp_servers = data.get("mcp_servers")
    if mcp_servers is None:
        mcp_servers = {}
        data["mcp_servers"] = mcp_servers
    mcp_servers["botserver"] = {
        "command": python,
        "args": ["-m", "bot.mcp_server"],
        "env": env_vars,
        "timeout": 180,
        "connect_timeout": 60,
    }

    _atomic_write_yaml(path, data, yaml)

    db.log_audit(actor=actor, action="hermes_mcp_register", detail=f"{path}: botserver -> {python}")
    logger.info("registered botserver MCP server at %s -> %s", path, python)
    return {"name": "botserver", "command": python, "config_path": str(path)}


def _project_root():
    from bot.envfile import PROJECT_ROOT

    return PROJECT_ROOT
