"""External MCP client — lets BotServer's own native agent loop use
tools from a third-party MCP server (a local stdio subprocess, or a
remote Streamable HTTP endpoint), the mirror image of bot/mcp_server.py
(which makes BotServer itself an MCP *server* for Claude Desktop/Hermes).

Deliberately human/operator-configured only, never agent-creatable —
connecting to an arbitrary external process or URL is a materially
bigger trust boundary than create_plugin's already-approval-gated local
code (bot/plugins.py, docs/adr/0007), so external_mcp_servers rows are
only ever written by a dashboard route or the /mcp_external Telegram
command, never by a tool call in TOOL_SCHEMAS. This is a genuinely
different concern from the existing /mcp command, which manages Claude
Desktop's own MCP server list for the `ui` backend.

One long-lived connection per enabled row, held open in an
AsyncExitStack for the life of the process (or until explicitly
reconnected/disconnected) — reused across every list_tools()/call_tool()
call rather than reconnecting per call. A connection failure (bad
command, unreachable URL, handshake error) is logged once and that
server's tools are simply omitted from the merged schema list; it never
raises into all_tool_schemas()/execute_tool()'s caller — an external
server being down must never break the whole tool-calling turn.

Known scope limitation, stated plainly rather than silently assumed:
external_mcp_servers.instance_id exists so a server can eventually be
scoped to one bot instance, but bot.agent_runtime.tools.all_tool_schemas()
has no instance_id parameter today — every connected (enabled) server's
tools are offered to every instance for now, regardless of that column's
value. Narrowing this is a follow-up once all_tool_schemas() itself
threads instance_id through, not attempted here.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("bot.agent_runtime.mcp_client")

TOOL_PREFIX = "mcp_"


@dataclass
class _Connection:
    name: str
    stack: AsyncExitStack
    session: Any  # mcp.ClientSession
    tools: list[dict] = field(default_factory=list)  # raw mcp.types.Tool-shaped dicts


# Process-wide, keyed by external_mcp_servers.name — mirrors
# bot/agent_runtime/subagent_registry.py's own process-wide-dict
# precedent for live, non-DB-backed connection state.
_connections: dict[str, _Connection] = {}
# namespaced tool name ("mcp_<server>_<tool>") -> (server_name, real_tool_name)
# — built alongside external_tool_schemas() so call_tool() never has to
# guess a reversible split of an underscore-containing server/tool name.
_tool_index: dict[str, tuple[str, str]] = {}


def _row_to_kwargs(row) -> dict:
    return {
        "transport": row["transport"], "command": row["command"],
        "args": json.loads(row["args_json"] or "[]"), "env": json.loads(row["env_json"] or "{}"),
        "url": row["url"], "auth_token": row["auth_token"],
    }


async def _open_session(name: str, *, transport: str, command, args, env, url, auth_token) -> _Connection:
    stack = AsyncExitStack()
    try:
        if transport == "stdio":
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            if not command:
                raise ValueError("stdio transport requires a command")
            params = StdioServerParameters(command=command, args=args or [], env=env or None)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
        elif transport == "remote":
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            if not url:
                raise ValueError("remote transport requires a url")
            http_client = None
            if auth_token:
                import httpx2

                http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {auth_token}"})
                await stack.enter_async_context(http_client)
            streams = await stack.enter_async_context(streamable_http_client(url, http_client=http_client))
            read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write))
        else:
            raise ValueError(f"unknown external MCP transport {transport!r} — expected 'stdio' or 'remote'")

        await session.initialize()
        listed = await session.list_tools()
        tools = [
            # mcp 2.0.0's real field name is the snake_case `input_schema`
            # (confirmed against the installed package — older mcp
            # releases used camelCase `inputSchema`).
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema or {"type": "object", "properties": {}}}
            for t in listed.tools
        ]
        return _Connection(name=name, stack=stack, session=session, tools=tools)
    except Exception:
        await stack.aclose()
        raise


async def connect(name: str) -> bool:
    """(Re)connects to the named, already-configured external server —
    closes any existing connection for this name first. Returns whether
    it succeeded; never raises (a bad config/unreachable server is a
    normal, expected outcome here, not a bug)."""
    from bot import db

    await disconnect(name)
    row = db.get_external_mcp_server(name)
    if row is None or not row["enabled"]:
        return False
    try:
        conn = await _open_session(name, **_row_to_kwargs(row))
    except Exception as exc:
        logger.warning("external MCP server %r: connection failed — its tools are unavailable this run (%s)", name, exc)
        return False
    _connections[name] = conn
    _rebuild_tool_index()
    logger.info("external MCP server %r: connected, %d tool(s)", name, len(conn.tools))
    return True


async def disconnect(name: str) -> None:
    conn = _connections.pop(name, None)
    if conn is not None:
        try:
            await conn.stack.aclose()
        except Exception:
            logger.exception("external MCP server %r: error while closing connection", name)
    _rebuild_tool_index()


async def disconnect_all() -> None:
    for name in list(_connections):
        await disconnect(name)


async def connect_all_enabled() -> None:
    """Called once at startup (mirrors bot/plugins.py::load_enabled()) —
    connects every enabled configured server, skipping (with a logged
    warning, never a crash) any that fail."""
    from bot import db

    for row in db.list_external_mcp_servers():
        if row["enabled"]:
            await connect(row["name"])


def _rebuild_tool_index() -> None:
    _tool_index.clear()
    for server_name, conn in _connections.items():
        for tool in conn.tools:
            _tool_index[f"{TOOL_PREFIX}{server_name}_{tool['name']}"] = (server_name, tool["name"])


def external_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-shaped {name, description, input_schema} entries for
    every tool on every currently-connected server, namespaced
    "mcp_<server>_<tool>" to avoid colliding with built-ins/plugins."""
    schemas = []
    for server_name, conn in _connections.items():
        for tool in conn.tools:
            schemas.append({
                "name": f"{TOOL_PREFIX}{server_name}_{tool['name']}",
                "description": f"[{server_name}] {tool['description']}",
                "input_schema": tool["input_schema"],
            })
    return schemas


def has_tool(name: str) -> bool:
    return name in _tool_index


async def call_tool(name: str, arguments: dict) -> str:
    server_name, real_name = _tool_index[name]
    conn = _connections.get(server_name)
    if conn is None:
        raise RuntimeError(f"external MCP server {server_name!r} is no longer connected")
    result = await conn.session.call_tool(real_name, arguments or {})
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    text_out = "\n".join(parts) if parts else "(no output)"
    if getattr(result, "is_error", False):
        return f"[tool error] {text_out}"
    return text_out


def connected_servers() -> list[str]:
    return sorted(_connections)
