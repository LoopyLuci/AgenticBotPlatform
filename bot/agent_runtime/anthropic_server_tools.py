"""Anthropic server-executed tools — web_search, web_fetch, code_execution,
tool_search (Phase D of the Claude API/Claude Code parity plan).

Anthropic runs these itself and returns results inline in the same
response; a server tool's call appears as a `server_tool_use` content
block, structurally distinct from the `tool_use` block a client tool
call uses (confirmed live against platform.claude.com/docs's real
example response shapes) — so these never reach
`bot.agent_runtime.tool_loop.run_one_tool()` by construction, and no
dispatch code is needed anywhere in `native_backend.py` for them.
`AnthropicTransport.send()` (`bot/agent_runtime/transports/anthropic.py`)
is the only wiring point: it appends `enabled_tool_entries()` to the
request's own `tools` array alongside BotServer's client tool schemas.

Off by default per-tool (`config/backends.yaml`'s `native_agent.server_tools`)
— each bills real usage-based cost on top of tokens (e.g. web search is
$10/1,000 searches), matching this project's own "safe by default,
explicit opt-in for anything with a real cost/trust implication"
convention (`native_agent.mcp_sampling` uses the same pattern).

Tool-type version strings confirmed live against the real tool-reference
page (`platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference`)
— each picked as that tool's newest documented stable dated version, or
for tool search, the undated `tool_search_tool_regex` alias the docs
themselves say resolves to the latest dated version (avoiding a stale
pin as Anthropic ships new dated releases).
"""

from __future__ import annotations

# name (the config key BotServer's own operator toggles) -> the real
# Anthropic tool-type string sent on the wire.
TOOL_TYPES: dict[str, str] = {
    "web_search": "web_search_20260318",
    "web_fetch": "web_fetch_20260318",
    "code_execution": "code_execution_20260521",
    "tool_search": "tool_search_tool_regex",
}


def _server_tools_config() -> dict:
    from bot.config import config

    return config.current.get("native_agent", {}).get("server_tools", {}) or {}


def enabled_tool_entries() -> list[dict]:
    """Anthropic-shaped {"type": ..., "name": ...} entries for every
    server tool this instance's config has opted into — appended to the
    request's own `tools` array by AnthropicTransport.send(). Every
    server tool is off unless explicitly enabled; an unrecognized key in
    config is silently ignored rather than raising, matching this
    codebase's general stance that a bad/stale config value shouldn't
    break a live turn."""
    cfg = _server_tools_config()
    return [{"type": tool_type, "name": name} for name, tool_type in TOOL_TYPES.items() if cfg.get(name, False)]
