"""bot/wasm_plugins/tools.py - a plugin's tools, as agent tools.

A wasm plugin offers tools; the agent knows nothing about WebAssembly and should not have to.
This is the whole translation: each tool the plugin's manifest describes becomes one
registered tool (`bot/agent_runtime/toolspec.register`) with the plugin's own JSON schema, and
a call becomes one `invoke`.

What the plugin does *not* get is decided here as much as in the runtime:

* every tool is named `<plugin>_<tool>`, so two plugins can both offer `search` without one
  shadowing the other, and the model can see which plugin a tool came from;
* the spec is `permission="external"`, `read_only=False`, `concurrency_safe=False`. A wasm
  tool is an opaque call into a component: nothing here can tell whether it will change
  something, so it is treated as a change, which means the approval machinery asks first;
* the output ceiling is the plugin's own, checked after the call as well as inside the
  runtime, because a limit enforced in one of two places has a hole in it exactly where the
  other is not called.

Registration is by plugin name and reversible, so disabling a plugin takes its tools out of
the model's tool list without touching anything else.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from bot.agent_runtime import toolspec

logger = logging.getLogger("bot.wasm_plugins.tools")

#: plugin name -> {tool name -> registered tool name}
_registered: dict = {}


def tool_name_for(plugin: str, tool: str) -> str:
    """The agent-facing name for one of a plugin's tools."""
    return f"{plugin}_{tool}".replace("-", "_")


def register_plugin(plugin: str, manifest: dict) -> None:
    """Register every tool in a plugin's manifest, and unregister whatever it had before.

    Re-registering rather than refusing to: a plugin can be reinstalled with a new version,
    and the tools of the old one must not stay behind with a stale schema.
    """
    unregister_plugin(plugin)
    names: dict = {}
    for tool in manifest.get("tools", []):
        raw = str(tool.get("name") or "").strip()
        if not raw:
            continue
        name = tool_name_for(plugin, raw)
        if name in names:
            # The plugin described two tools that collapse to the same agent-facing name.
            logger.warning("wasm plugin %s: tools %r collide as %s; the second is not registered", plugin, raw, name)
            continue
        schema = _schema_for(tool, raw)
        try:
            toolspec.register(
                {
                    "name": name,
                    "description": _description_for(plugin, raw, tool),
                    "input_schema": schema,
                },
                toolspec.ToolSpec(name=name, permission="external", read_only=False, origin="plugin"),
                _handler(plugin, raw),
            )
        except ValueError as exc:
            # A tool name that collides with a built-in, or with another plugin's, is refused
            # rather than allowed to shadow it.
            logger.error("wasm plugin %s: tool %s not registered: %s", plugin, name, exc)
            continue
        names[raw] = name
    _registered[plugin] = names
    if names:
        logger.info("wasm plugin %s registered %d tool(s): %s", plugin, len(names), ", ".join(sorted(names.values())))


def unregister_plugin(plugin: str) -> None:
    """Take one plugin's tools out of the agent's tool list."""
    names = _registered.pop(plugin, {})
    for name in names.values():
        toolspec.unregister(name)
    if names:
        logger.info("wasm plugin %s: unregistered %d tool(s)", plugin, len(names))


def _handler(plugin: str, tool: str):
    """The async handler the agent loop calls. Matches the built-ins exactly:
    `async def handler(tool_input, *, workspace, instance_id, device_tier) -> str`."""

    async def handler(tool_input: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
        from bot.wasm_plugins import manager

        return manager.invoke(plugin, tool, tool_input)

    handler.__name__ = f"wasm_{plugin}_{tool}"
    return handler


def _description_for(plugin: str, tool: str, entry: dict) -> str:
    description = (entry.get("description") or "").strip()
    return description or f"The {tool} tool, provided by the {plugin} WebAssembly plugin."


def _schema_for(entry: dict, tool: str) -> dict:
    """The plugin's own JSON schema, as a tool schema.

    A plugin's `schema` field is a JSON Schema as text. It is used as given when it parses and
    is an object; when it does not, a permissive one is substituted so the model still sees
    the tool. Substituting is better than refusing: a plugin with a malformed schema is a bug
    in that plugin, and a tool with a loose schema is far less bad than a plugin whose tools
    are all missing.
    """
    raw = entry.get("schema") or ""
    try:
        schema = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    except json.JSONDecodeError:
        schema = None
    if isinstance(schema, dict) and schema.get("type") == "object":
        schema.setdefault("properties", {})
        return schema
    logger.warning("wasm plugin tool %s: its schema is not a JSON Schema object; using a permissive one", tool)
    return {"type": "object", "properties": {}, "additionalProperties": True}


def has_tool(name: str) -> bool:
    return any(name in names.values() for names in _registered.values())


def tool_schemas() -> list[dict]:
    return toolspec.registered_schemas()


def plugin_for_tool(name: str) -> Optional[str]:
    """Which plugin a registered tool name belongs to, or None."""
    for plugin, names in _registered.items():
        if name in names.values():
            return plugin
    return None


def registered() -> dict:
    """plugin name -> the tool names registered for it. For the dashboard and the tests."""
    return {plugin: dict(names) for plugin, names in _registered.items()}


def reset_for_tests() -> None:
    for plugin in list(_registered):
        unregister_plugin(plugin)
