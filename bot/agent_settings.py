"""Unified agent/swarm control settings — one real place to see and
change how many subagents run in parallel, what provider/model workers
use, and what "effort" (see bot/effort.py) both workers and the
manager/orchestrator itself think at. Closes the standing gap where
these were only reachable as scattered tool-call arguments
(spawn_subagent's own provider/model/max_children) or a raw
config/backends.yaml edit, with no discoverable settings surface.

Resolution is three levels, each falling through to the next only when a
field is genuinely unset (None), never when it's a falsy-but-real value:
1. This instance's own row (agent_settings.instance_id = the instance).
2. The process-wide default row (agent_settings.instance_id IS NULL).
3. Hardcoded constants — DEFAULT_MAX_CONCURRENT_CHILDREN (matching
   bot/agent_runtime/subagents.py's own existing constant) and no effort
   override at all (None), so an instance with nothing configured
   anywhere behaves exactly as it did before this module existed.

Hermes-backed instances do NOT store worker_effort/manager_effort here —
see bot/hermes_config.py's set_delegation_config/set_agent_config, which
write straight into Hermes's own config.yaml (the real, single source of
truth for that backend family). This table only ever governs
api/custom_model/native_agent instances' own subagent fan-out.
"""

from __future__ import annotations

from typing import Any, Optional

from bot import db
from bot.agent_runtime.subagents import DEFAULT_MAX_CONCURRENT_CHILDREN

FIELDS = (
    "max_concurrent_children", "worker_provider", "worker_model", "worker_effort", "manager_effort",
    "fallback_provider", "fallback_model",
)


def _hardcoded_default(field: str) -> Any:
    if field == "max_concurrent_children":
        # config/backends.yaml's native_agent.max_concurrent_children was
        # the only lever for this before agent_settings existed — kept as
        # the next fallback below the new settings table (rather than
        # replaced outright) so an operator's existing config.yaml value
        # keeps working exactly as it did, for anyone who never touches
        # the new settings surface at all.
        from bot.config import config

        return config.current.get("native_agent", {}).get("max_concurrent_children", DEFAULT_MAX_CONCURRENT_CHILDREN)
    return None


def get(instance_id: Optional[int]) -> dict[str, Any]:
    """Fully-resolved settings for [instance_id] — every key in FIELDS is
    always present, with the fallback chain already applied: this
    instance's own row -> the process-wide default row ->
    config/backends.yaml (max_concurrent_children only) -> None."""
    own = db.get_agent_settings_row(instance_id) if instance_id is not None else None
    default_row = db.get_agent_settings_row(None)

    resolved: dict[str, Any] = {}
    for field in FIELDS:
        value = own[field] if own is not None else None
        if value is None and default_row is not None:
            value = default_row[field]
        if value is None:
            value = _hardcoded_default(field)
        resolved[field] = value
    return resolved


def set_settings(instance_id: Optional[int], **fields: Any) -> dict[str, Any]:
    """Merges only the given fields into [instance_id]'s row (None
    instance_id targets the process-wide default row). Pass a field as
    None explicitly to clear it back to "fall through to the next level"
    — omitting a field entirely leaves it untouched, matching every other
    partial-update function in this codebase (e.g. bot_instances.update_instance).
    Named set_settings rather than the bare "set" to avoid shadowing the
    builtin this module doesn't otherwise need, but easily could
    elsewhere."""
    unknown = [f for f in fields if f not in FIELDS]
    if unknown:
        raise ValueError(f"unknown agent_settings field(s): {sorted(unknown)}")
    if fields:
        db.set_agent_settings_row(instance_id, **fields)
    return get(instance_id)
