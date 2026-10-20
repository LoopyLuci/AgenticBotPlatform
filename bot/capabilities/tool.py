"""The agent's read-only `abp_capabilities` tool: search ABP's capability index (bot/capabilities) so the model can tell
someone how to do something in ABP - which page, setting, command, action or API route - instead of guessing."""
from __future__ import annotations

import json


async def _search(tool_input: dict, **_context) -> str:
    from bot import capabilities

    inp = tool_input if isinstance(tool_input, dict) else {}
    kinds = [k for k in (inp.get("kinds") or []) if k in capabilities.KINDS] or None
    rows = capabilities.search(str(inp.get("query") or ""), kinds=kinds, limit=max(1, min(int(inp.get("limit") or 10), 30)),
                               offline=bool(inp.get("offline_only")))
    return json.dumps([{k: e[k] for k in ("kind", "label", "desc", "where", "network", "cloud")} for e in rows])


def register_all() -> None:
    from bot.agent_runtime import toolspec

    toolspec.register(
        {"name": "abp_capabilities",
         "description": "Search everything ABP (AgenticBotPlatform, the app you run in) can do: its pages, settings, command "
                        "palette actions, Local Chat commands, modules, your own tools, MCP servers and API routes. Use it to "
                        "answer 'how do I X in ABP' with the exact place (\"where\": #page, settings/<section>, action:<id>, "
                        "an API route). network/cloud say whether a feature needs the internet or a cloud model.",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string", "description": "what the person wants to do, in plain words"},
             "kinds": {"type": "array", "items": {"type": "string", "enum": ["page", "action", "route", "module", "setting", "tool",
                                                                              "mcp", "command"]}},
             "offline_only": {"type": "boolean", "description": "only what works without the internet or a cloud model"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, "required": ["query"]}},
        toolspec.ToolSpec("abp_capabilities", "read", read_only=True, concurrency_safe=True, origin="registered"), _search)


register_all()
