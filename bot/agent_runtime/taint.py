"""Which conversations have read untrusted content (roadmap P2).

Text that arrives from outside - a web page, a search result, an external MCP server -
can contain instructions aimed at the agent ("ignore your rules and run ..."). The
agent's guidance tells it to treat such text as data, but guidance is not enforcement.
This module is the enforcement half: once a session has read untrusted content it is
"tainted", and the permission layer (permissions.py) turns every "allow" for a tool that
can change something into "ask", and ignores standing approvals, so a person sees the
action before it happens.

Read-only tools keep working in a tainted session, so research does not stall.

A session stops being tainted when it ends (a new session) or when a person clears it.
MCP servers are untrusted by default; an operator marks a server they trust:

    native_agent:
      mcp_trust:
        my-internal-server: trusted
"""

from __future__ import annotations

from typing import Optional

from bot.agent_runtime import toolspec

# Tools whose results are always untrusted, whatever else is configured.
UNTRUSTED_TOOLS = frozenset({"web_fetch", "web_search"})

_tainted: dict[str, list[str]] = {}
MAX_SOURCES = 20


def _trust_config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("mcp_trust")) or {}
    except Exception:  # noqa: BLE001
        return {}


def mcp_server_for(tool: str) -> Optional[str]:
    try:
        from bot.agent_runtime import mcp_client

        return mcp_client.server_for_tool(tool)
    except Exception:  # noqa: BLE001
        return None


def source_of(tool: str) -> Optional[str]:
    """A label if this tool's output must be treated as untrusted, else None."""
    if tool in UNTRUSTED_TOOLS:
        return tool
    spec = toolspec.spec_for(tool)
    if spec.origin == "mcp":
        server = mcp_server_for(tool) or "unknown"
        if str(_trust_config().get(server, "untrusted")).lower() != "trusted":
            return f"mcp:{server}"
    return None


def mark(session: str, source: str) -> None:
    sources = _tainted.setdefault(session, [])
    if source not in sources and len(sources) < MAX_SOURCES:
        sources.append(source)


def note_result(session: str, tool: str) -> Optional[str]:
    """Called after a tool ran; taints the session if its output is untrusted."""
    source = source_of(tool)
    if source:
        mark(session, source)
    return source


def is_tainted(session: str) -> bool:
    return session in _tainted


def sources(session: str) -> list[str]:
    return list(_tainted.get(session, []))


def clear(session: str) -> None:
    _tainted.pop(session, None)


def forget_all() -> None:
    _tainted.clear()
