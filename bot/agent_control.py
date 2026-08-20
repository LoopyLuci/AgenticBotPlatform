"""Cross-agent permission checks — can one bot instance ask/command another?

Two modes, toggled globally at config/backends.yaml's agent_control.mode:
  trust_all  (default) — any instance may target any other, no restrictions.
  allowlist  — an instance may only target ids listed in its own can_target
               column (bot_instances.can_target, a JSON array).

Source-instance identity for cross-agent calls is self-declared by the
caller (see bot/mcp_server.py's ask_instance/run_swarm docstrings) — not
cryptographically verified. Acceptable for this app's single-operator
trust model, where the dashboard token is the real perimeter.
"""

from __future__ import annotations

from typing import Any, Optional

from bot import bot_instances
from bot.config import config


def current_mode() -> str:
    return config.current.get("agent_control", {}).get("mode", "trust_all")


def resolve_instance(name_or_id: Any) -> Optional[dict[str, Any]]:
    """Accepts either a bot_instances.id (int or numeric string) or a
    .name string — MCP callers naturally think in names."""
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
        return bot_instances.get_instance(int(name_or_id))
    for inst in bot_instances.list_instances():
        if inst["name"] == name_or_id:
            return inst
    return None


def can_target(source_instance_id: int, target_instance_id: int) -> bool:
    if current_mode() != "allowlist":
        return True
    source = bot_instances.get_instance(source_instance_id)
    if source is None:
        return False
    return target_instance_id in (source.get("can_target") or [])
