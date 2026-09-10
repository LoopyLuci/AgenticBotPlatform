"""Knowledge Modules — the static grouping of every Support Bot intent
into an independently trainable/toggleable unit.

This is pure data plus lookup helpers: no I/O, no classifier math, no
runtime state. It exists so a later Knowledge Module can be trained,
evaluated, retrained, enabled/disabled, or removed on its own, without
disturbing the other modules — see bot/support_bot/module_manifest.py for
the runtime enabled/disabled state, and bot/support_bot/cascade.py (a
later phase) for how modules are actually combined at classification
time.

Every intent in bot/support_bot/training_data.py's EXAMPLES must belong
to exactly one module here — see tests/test_support_bot_knowledge_modules.py's
self-check, which fails loudly (same convention as
bot/hotreload.py's own "every bot/*.py module is classified exactly
once" test) if a newly-added intent is forgotten here.

`ADMIN_ONLY_INTENTS`/`DESTRUCTIVE_INTENTS` (training_data.py) stay
completely orthogonal to this grouping — a module is a *training/
deployment* unit, a permission tag is a *runtime authorization* concern,
and the two are not meant to line up 1:1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ModuleSpec:
    module_id: str
    display_name: str
    description: str
    intents: tuple[str, ...]
    # core_status is the only always-resident module: it's the fallback
    # that answers "what can you do" / "status", and must exist even if
    # every other module has been unloaded — see module_manifest.py's
    # set_enabled(), which refuses to disable an unloadable module.
    unloadable: bool = field(default=True)


MODULE_REGISTRY: dict[str, ModuleSpec] = {
    spec.module_id: spec
    for spec in (
        ModuleSpec(
            "core_status", "Core Status",
            "Always-resident fallback — server status and self-help.",
            ("status", "help"),
            unloadable=False,
        ),
        ModuleSpec(
            "bots", "Bot Instances",
            "Creating, editing, enabling/disabling, restarting, and deleting bot instances.",
            ("list_bots", "bot_create", "bot_edit", "bot_delete", "bot_enable", "bot_disable", "bot_restart"),
        ),
        ModuleSpec(
            "routing", "Backend & Model Routing",
            "Which backend/model handles requests, and general settings.",
            ("backend_show", "backend_set", "model_show", "model_set", "settings_show", "settings_set"),
        ),
        ModuleSpec(
            "mcp", "MCP Servers",
            "Listing, enabling/disabling, and viewing logs for MCP servers.",
            ("mcp_list", "mcp_enable", "mcp_disable", "mcp_logs"),
        ),
        ModuleSpec(
            "desktop_app", "Claude Desktop & App Updates",
            "Starting/stopping/restarting Claude Desktop, config reload, app updates.",
            ("desktop_start", "desktop_stop", "desktop_restart", "config_reload", "app_update"),
        ),
        ModuleSpec(
            "jobs_swarms", "Jobs & Swarms",
            "Job queue/status and swarm run/status.",
            ("jobs_list", "job_status", "swarms_list", "swarm_run", "swarm_run_status"),
        ),
        ModuleSpec(
            "diagnostics_db", "Diagnostics & Database",
            "Error/latency diagnostics, database size and vacuum.",
            ("diagnostics", "db_status", "db_vacuum"),
        ),
        ModuleSpec(
            "backups", "Backups",
            "Listing and restoring env/bot-instance backups.",
            ("backups_list", "backup_restore"),
        ),
        ModuleSpec(
            "devices_security", "Devices & Security",
            "Paired devices, permission tiers, pairing keys, and the allowlist.",
            ("devices_list", "device_revoke", "device_retier", "mobile_key_create", "allowed_users_list"),
        ),
        ModuleSpec(
            "sessions", "Sessions",
            "Listing and viewing past conversation sessions.",
            ("sessions_list", "session_show"),
        ),
        ModuleSpec(
            "setup_checks", "Setup Checks",
            "Verifying the Claude Desktop and Hermes backend connections.",
            ("claude_setup_check", "hermes_setup_check"),
        ),
        ModuleSpec(
            "estop", "Emergency Stop",
            "Checking, engaging, and disengaging the emergency stop.",
            ("estop_status", "estop_engage", "estop_disengage"),
        ),
        ModuleSpec(
            "hooks", "Hooks",
            "Listing, enabling/disabling, and removing agent hooks.",
            ("hooks_list", "hook_enable", "hook_disable", "hook_remove"),
        ),
        ModuleSpec(
            "agent_config", "Agent Configuration",
            "Per-instance agent settings, effort level, and auto-manage.",
            ("agent_settings_show", "agent_settings_set_effort", "auto_manage_show"),
        ),
    )
}


def _build_intent_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for spec in MODULE_REGISTRY.values():
        for intent in spec.intents:
            index[intent] = spec.module_id
    return index


# intent -> module_id, built once at import time from the registry above.
_INTENT_TO_MODULE: dict[str, str] = _build_intent_index()


def module_for_intent(intent: str) -> Optional[str]:
    """The module_id an intent belongs to, or None if the intent isn't in
    the registry at all (e.g. a stale/removed intent in old DB rows)."""
    return _INTENT_TO_MODULE.get(intent)


def intents_for_module(module_id: str) -> tuple[str, ...]:
    spec = MODULE_REGISTRY.get(module_id)
    return spec.intents if spec is not None else ()


def all_module_ids() -> list[str]:
    return list(MODULE_REGISTRY.keys())


def all_intents() -> set[str]:
    return set(_INTENT_TO_MODULE.keys())
