"""What the agent runtime knows *about* a tool, apart from how it runs.

`tools.py` holds the built-in tools' schemas and handlers. This module adds the
facts the loop, the permission layer and the eval harness need to reason about a
call without executing it:

* `permission` — what kind of thing the tool does (read / write / execute /
  agent / config / admin / external), the basis for permission rules;
* `read_only` and `concurrency_safe` — whether the loop may run several calls at
  once (P1 parallel tool calls) and whether a call can change anything;
* `max_output_chars` — a ceiling on what a call may put back into the context.

Built-in tools are described by a table here, checked by a test so that a new tool
cannot be added to `TOOL_SCHEMAS` without saying what it is. Plugin and MCP tools
get conservative defaults. New first-class tools (P1 onward) register through
`register()` with a schema, a spec and a handler in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

PERMISSIONS = ("read", "write", "execute", "network", "agent", "config", "admin", "external")

# Bound on what one call may return into the model's context, unless a tool says
# otherwise. Built-ins already truncate their own output; this is the backstop for
# plugin and MCP tools, which used to be unbounded.
DEFAULT_MAX_OUTPUT_CHARS = 30_000

Handler = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    permission: str = "external"
    read_only: bool = False
    concurrency_safe: bool = False
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    origin: str = "builtin"          # builtin | registered | plugin | mcp

    def __post_init__(self) -> None:
        if self.permission not in PERMISSIONS:
            raise ValueError(f"unknown permission class {self.permission!r} for tool {self.name!r}")
        if self.concurrency_safe and not self.read_only:
            raise ValueError(f"tool {self.name!r}: only a read-only tool can be concurrency-safe")


def _reads(*names: str) -> dict[str, tuple[str, bool, bool]]:
    return {n: ("read", True, True) for n in names}


def _tools(permission: str, *names: str) -> dict[str, tuple[str, bool, bool]]:
    return {n: (permission, False, False) for n in names}


# name -> (permission, read_only, concurrency_safe)
_BUILTIN: dict[str, tuple[str, bool, bool]] = {
    **_reads("read_file", "list_dir", "git_status", "git_diff", "read_skill", "list_skills", "list_plugins",
             "kanban_list_cards", "list_schedules", "list_subagents", "check_batch_status", "get_batch_results",
             "get_my_profile", "read_project_context", "list_project_context",
             "admin_list_bot_instances", "admin_get_bot_instance", "admin_get_agent_settings",
             "admin_get_auto_manage_config", "admin_get_estop_status", "admin_list_hooks", "admin_list_devices"),
    **_tools("execute", "run_shell"),
    **_tools("write", "write_file", "save_memory", "install_skill", "create_skill", "remove_skill",
             "kanban_add_card", "kanban_move_card", "write_project_context"),
    **_tools("execute", "create_plugin", "enable_plugin", "disable_plugin", "remove_plugin"),
    **_tools("agent", "delegate_to_instance", "spawn_subagent", "steer_subagent", "stop_subagent", "consult_models",
             "dispatch_batch_completions"),
    **_tools("config", "schedule_command", "pause_schedule", "remove_schedule", "update_agent_config"),
    **_tools("admin", "admin_create_bot_instance", "admin_update_bot_instance", "admin_delete_bot_instance",
             "admin_set_default_backend", "admin_set_agent_settings", "admin_set_auto_manage_config",
             "admin_engage_estop", "admin_disengage_estop", "admin_add_hook", "admin_enable_hook",
             "admin_disable_hook", "admin_remove_hook", "admin_mint_device_key", "admin_set_device_tier",
             "admin_revoke_device", "admin_db_vacuum", "admin_restore_snapshot", "admin_desktop_start",
             "admin_desktop_stop", "admin_desktop_restart"),
}

_registered: dict[str, tuple[dict, ToolSpec, Handler]] = {}


def builtin_names() -> frozenset[str]:
    return frozenset(_BUILTIN)


def spec_for(name: str) -> ToolSpec:
    """The spec for any tool name. Unknown names are treated as external and
    non-read-only — the conservative reading for something we know nothing about."""
    if name in _registered:
        return _registered[name][1]
    if name in _BUILTIN:
        permission, read_only, concurrency_safe = _BUILTIN[name]
        return ToolSpec(name, permission, read_only, concurrency_safe)
    origin = "plugin" if _is_plugin(name) else "mcp" if _is_mcp(name) else "external"
    return ToolSpec(name, "external", origin=origin)


def _is_plugin(name: str) -> bool:
    try:
        from bot import plugins

        return bool(plugins.has_tool(name))
    except Exception:  # noqa: BLE001
        return False


def _is_mcp(name: str) -> bool:
    try:
        from bot.agent_runtime import mcp_client

        return bool(mcp_client.has_tool(name))
    except Exception:  # noqa: BLE001
        return False


def is_read_only(name: str) -> bool:
    return spec_for(name).read_only


def is_concurrency_safe(name: str) -> bool:
    return spec_for(name).concurrency_safe


def limit_output(name: str, text: str) -> str:
    """Cap what a tool call puts into the context. Cuts at the ceiling and says so,
    keeping the tail too — for command and log output the end is usually what matters."""
    cap = spec_for(name).max_output_chars
    if not isinstance(text, str) or len(text) <= cap:
        return text
    head = int(cap * 0.7)
    tail = cap - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted} characters omitted] …\n{text[-tail:]}"


# ---- first-class registered tools (P1 onward) ---------------------------------
def register(schema: dict, spec: ToolSpec, handler: Handler) -> None:
    """Add a tool: its schema, its spec and its handler in one place. The handler is
    `async def handler(tool_input, *, workspace, instance_id, device_tier) -> str`."""
    if schema.get("name") != spec.name:
        raise ValueError("schema name and spec name differ")
    if spec.name in _BUILTIN:
        raise ValueError(f"{spec.name!r} is already a built-in tool")
    _registered[spec.name] = (schema, spec, handler)


def unregister(name: str) -> None:
    _registered.pop(name, None)


def registered_schemas() -> list[dict[str, Any]]:
    return [entry[0] for entry in _registered.values()]


def has_handler(name: str) -> bool:
    return name in _registered


async def dispatch(name: str, tool_input: dict, **context: Any) -> str:
    return await _registered[name][2](tool_input, **context)


def registered_dangerous(name: str) -> bool:
    """A registered tool needs approval unless it is read-only."""
    entry: Optional[tuple] = _registered.get(name)
    return bool(entry and not entry[1].read_only)
