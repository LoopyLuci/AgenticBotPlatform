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

Also implements the two other real MCP client capabilities beyond tool-
calling, both real-API-confirmed against the installed mcp==2.0.0
package before writing any of this:

- **Sampling**: a connected server can ask BotServer to run a real LLM
  completion on its behalf (`ClientSession(sampling_callback=...)`).
  Off by default (`config/backends.yaml`'s `native_agent.mcp_sampling.enabled`)
  — unlike calling a server's own advertised tools, this spends the
  operator's own configured provider budget with no per-call human step,
  a materially different trust question than "I already chose to connect
  this server." Confirmed live (a real stdio server subprocess genuinely
  calling `ServerSession.create_message()` and getting a real routed
  response back) that `sampling_callback` is still the correct, current
  client-side hook in mcp==2.0.0 despite that server-side convenience
  method itself carrying an `MCPDeprecationWarning` (SEP-2577 renames the
  server's own calling convention to `ClientPeer.sample()`; the
  underlying request this callback answers is unchanged either way).
- **OAuth 2.1** (dynamic client registration + authorization code + PKCE)
  for a `remote` server that requires it instead of a static bearer
  token, via `mcp.client.auth.OAuthClientProvider` (an `httpx2.Auth`
  subclass — plugged into the same `http_client=` the bearer-token path
  already uses). Tokens/client info persist in `external_mcp_servers`'
  `oauth_*` columns via `_DbTokenStorage`. The interactive step (opening
  a browser to grant consent) has nobody synchronously present to click
  through — `connect()` surfaces the authorization URL via
  `oauth_authorization_url()` (shown by the dashboard/`/mcp_external`
  command) and blocks, inside `session.initialize()`, until
  `deliver_oauth_callback()` is called by the dashboard's own
  `GET /api/mcp-external/oauth/callback` route (which the operator's
  browser lands on after granting consent) or `OAUTH_CALLBACK_TIMEOUT_S`
  elapses. Elicitation (a server prompting the user for input mid-tool-
  call) remains deferred — BotServer's turn-based chat model has no
  obvious place for a server-initiated mid-turn prompt, a materially
  harder design question than either of the two capabilities above.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("bot.agent_runtime.mcp_client")

TOOL_PREFIX = "mcp_"
DEFAULT_SAMPLING_MODEL = "claude-sonnet-5"
MAX_SAMPLING_TOKENS = 4096
SAMPLING_TIMEOUT_S = 120.0
OAUTH_CALLBACK_TIMEOUT_S = 300.0


@dataclass
class _Connection:
    """Deliberately holds no AsyncExitStack of its own — anyio's cancel
    scopes are bound to the asyncio Task that entered them, so the stack
    that opened this connection (inside _connection_owner's own task)
    must also be the one that closes it; a disconnect() called from a
    different task raises "Attempted to exit cancel scope in a different
    task than it was entered in" (found via direct live reproduction
    while wiring the background-task connect() below). close_event is
    disconnect()'s only lever: set it, then wait for _connection_owner's
    task (which is awaiting this same event) to notice, close its own
    stack itself, and exit."""

    name: str
    session: Any  # mcp.ClientSession
    tools: list[dict] = field(default_factory=list)  # raw mcp.types.Tool-shaped dicts
    close_event: asyncio.Event = field(default_factory=asyncio.Event)


# Process-wide, keyed by external_mcp_servers.name — mirrors
# bot/agent_runtime/subagent_registry.py's own process-wide-dict
# precedent for live, non-DB-backed connection state.
_connections: dict[str, _Connection] = {}
# The asyncio.Task actually running _connection_owner() for each name —
# disconnect() needs this to await its real shutdown (or cancel it
# outright if it never got as far as registering a _Connection at all,
# e.g. still stuck waiting on an OAuth authorization).
_owner_tasks: dict[str, asyncio.Task] = {}
# namespaced tool name ("mcp_<server>_<tool>") -> (server_name, real_tool_name)
# — built alongside external_tool_schemas() so call_tool() never has to
# guess a reversible split of an underscore-containing server/tool name.
_tool_index: dict[str, tuple[str, str]] = {}

# server_name -> {"event": asyncio.Event, "url": str, "code": Optional[str],
# "state": Optional[str]} — one entry for as long as a browser-driven OAuth
# authorization is outstanding for that server. state -> server_name is
# kept separately because the dashboard's callback route only ever gets
# `state` back from the browser, never the server name.
_pending_oauth: dict[str, dict] = {}
_oauth_state_to_server: dict[str, str] = {}


def _row_to_kwargs(row) -> dict:
    return {
        "transport": row["transport"], "command": row["command"],
        "args": json.loads(row["args_json"] or "[]"), "env": json.loads(row["env_json"] or "{}"),
        "url": row["url"], "auth_token": row["auth_token"], "oauth_enabled": bool(row["oauth_enabled"]),
    }


class _DbTokenStorage:
    """mcp.client.auth.oauth2.TokenStorage, backed by this server's own
    external_mcp_servers row — so a re-`connect()` (a process restart, a
    manual reconnect) reuses a previously-granted token/refresh-token
    instead of asking the operator to authorize again every time."""

    def __init__(self, server_name: str):
        self.server_name = server_name

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        from bot import db

        row = db.get_external_mcp_server(self.server_name)
        if row is None or not row["oauth_tokens_json"]:
            return None
        return OAuthToken.model_validate_json(row["oauth_tokens_json"])

    async def set_tokens(self, tokens) -> None:
        from bot import db

        db.set_external_mcp_oauth_tokens(self.server_name, tokens.model_dump_json())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        from bot import db

        row = db.get_external_mcp_server(self.server_name)
        if row is None or not row["oauth_client_info_json"]:
            return None
        return OAuthClientInformationFull.model_validate_json(row["oauth_client_info_json"])

    async def set_client_info(self, client_info) -> None:
        from bot import db

        db.set_external_mcp_oauth_client_info(self.server_name, client_info.model_dump_json())


def _dashboard_oauth_redirect_uri() -> str:
    import os

    port = os.environ.get("DASHBOARD_PORT", "8787")
    return f"http://127.0.0.1:{port}/api/mcp-external/oauth/callback"


def oauth_authorization_url(name: str) -> Optional[str]:
    """The URL a human needs to open in a browser to authorize [name], if
    a flow is currently pending for it — surfaced by the dashboard's
    /api/mcp-external listing and the /mcp_external Telegram command so
    it's actually reachable, not just a log line."""
    pending = _pending_oauth.get(name)
    return pending["url"] if pending else None


def deliver_oauth_callback(state: str, code: str) -> bool:
    """Called by the dashboard's GET /api/mcp-external/oauth/callback
    route once the operator's browser lands back on it after granting
    consent. Returns whether a matching pending flow was found (a
    mismatch means the flow already timed out, or `state` is bogus)."""
    name = _oauth_state_to_server.get(state)
    pending = _pending_oauth.get(name) if name else None
    if pending is None:
        return False
    pending["code"] = code
    pending["state"] = state
    pending["event"].set()
    return True


def _clear_pending_oauth(name: str) -> None:
    _pending_oauth.pop(name, None)
    # Scanned rather than looked up via pending["state"] — that field is
    # only ever populated once deliver_oauth_callback() actually fires,
    # but _oauth_state_to_server's reverse entry exists from the moment
    # the redirect handler runs, well before that (a real bug found via
    # a direct test: clearing a flow that was never delivered left its
    # state entry stuck in the reverse map forever).
    stale_states = [state for state, server_name in _oauth_state_to_server.items() if server_name == name]
    for state in stale_states:
        _oauth_state_to_server.pop(state, None)


def _make_oauth_redirect_handler(name: str):
    async def _handler(url: str) -> None:
        state = parse_qs(urlparse(url).query).get("state", [None])[0]
        _pending_oauth[name] = {"event": asyncio.Event(), "url": url, "code": None, "state": None}
        if state:
            _oauth_state_to_server[state] = name
        logger.warning("external MCP server %r needs authorization — open this URL to continue: %s", name, url)

    return _handler


def _make_oauth_callback_handler(name: str):
    async def _handler():
        from mcp.shared.auth import AuthorizationCodeResult

        pending = _pending_oauth.get(name)
        if pending is None:
            raise RuntimeError(f"no pending OAuth authorization flow for {name!r}")
        try:
            await asyncio.wait_for(pending["event"].wait(), timeout=OAUTH_CALLBACK_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"timed out waiting for OAuth authorization for {name!r}") from exc
        return AuthorizationCodeResult(code=pending["code"], state=pending["state"])

    return _handler


def _sampling_config() -> dict:
    from bot.config import config

    return config.current.get("native_agent", {}).get("mcp_sampling", {}) or {}


def _resolve_sampling_transport(cfg: dict):
    provider = cfg.get("provider")
    model = cfg.get("model") or DEFAULT_SAMPLING_MODEL
    if not provider or provider == "anthropic":
        from bot.agent_runtime.transports.anthropic import AnthropicTransport

        return AnthropicTransport(), model
    from bot import providers as provider_registry
    from bot.agent_runtime.transports import build_openai_transport

    provider_cfg = provider_registry.get_provider(provider)
    if provider_cfg is None:
        raise ValueError(f"native_agent.mcp_sampling.provider {provider!r} is not a configured provider")
    transport = build_openai_transport(
        protocol=provider_cfg.get("protocol", "openai"), base_url=provider_cfg["base_url"],
        api_key=provider_registry.get_api_key(provider), catalog_id=provider_cfg.get("catalog_id"),
    )
    return transport, model


def _flatten_sampling_content(content) -> tuple[str, list[dict]]:
    parts = content if isinstance(content, list) else [content]
    texts: list[str] = []
    images: list[dict] = []
    for part in parts:
        part_type = getattr(part, "type", None)
        if part_type == "text":
            texts.append(part.text)
        elif part_type == "image":
            images.append({"mime_type": part.mime_type, "data_b64": part.data})
        # audio/tool_use/tool_result sampling-message content isn't
        # something any of our transports can represent — silently
        # dropped from the rendered prompt rather than raising, same
        # "best effort, never break the call" stance as the rest of this
        # module.
    return "\n".join(texts), images


async def handle_sampling_request(context, params):
    """The real SamplingFnT callback (mcp.client.session) — a connected
    server asking BotServer to run one LLM completion on its behalf.
    Never raises: any failure (disabled, misconfigured, transport error)
    comes back as a real MCP ErrorData, which is a normal, protocol-level
    outcome a well-behaved server already has to handle."""
    from mcp import types

    cfg = _sampling_config()
    if not cfg.get("enabled", False):
        return types.ErrorData(
            code=types.INVALID_REQUEST,
            message="MCP sampling is disabled for this BotServer install (native_agent.mcp_sampling.enabled)",
        )

    try:
        transport, model = _resolve_sampling_transport(cfg)
        history: list[dict] = []
        for message in params.messages:
            text, images = _flatten_sampling_content(message.content)
            if message.role == "assistant":
                history.append({"role": "assistant", "content": text})
            else:
                history.append(transport.user_message(text, images=images or None))
        response = await transport.send(
            model=model, history=history, tool_schemas=[],
            max_tokens=min(params.max_tokens or 1024, MAX_SAMPLING_TOKENS),
            timeout_s=SAMPLING_TIMEOUT_S, system_prompt=params.system_prompt,
        )
    except Exception as exc:
        logger.exception("MCP sampling request failed")
        return types.ErrorData(code=types.INTERNAL_ERROR, message=str(exc))

    return types.CreateMessageResult(
        role="assistant", content=types.TextContent(type="text", text=response.text or ""),
        model=model, stop_reason="endTurn",
    )


async def _build_session(
    stack: AsyncExitStack, name: str, *, transport: str, command, args, env, url, auth_token,
    oauth_enabled: bool = False,
) -> tuple[Any, list[dict]]:
    """The actual protocol setup, given a stack the CALLER owns (see
    _connection_owner below for why ownership matters) — opens the
    transport, wraps it in a ClientSession, initializes, and lists tools.
    Raises on any failure; the stack is the caller's to close either way."""
    if transport == "stdio":
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if not command:
            raise ValueError("stdio transport requires a command")
        params = StdioServerParameters(command=command, args=args or [], env=env or None)
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(
            ClientSession(read, write, sampling_callback=handle_sampling_request)
        )
    elif transport == "remote":
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if not url:
            raise ValueError("remote transport requires a url")
        http_client = None
        if oauth_enabled:
            import httpx2
            from mcp.client.auth import OAuthClientProvider
            from mcp.shared.auth import OAuthClientMetadata

            metadata = OAuthClientMetadata(
                redirect_uris=[_dashboard_oauth_redirect_uri()], client_name="BotServer",
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                token_endpoint_auth_method="none",
            )
            oauth_provider = OAuthClientProvider(
                server_url=url, client_metadata=metadata, storage=_DbTokenStorage(name),
                redirect_handler=_make_oauth_redirect_handler(name),
                callback_handler=_make_oauth_callback_handler(name),
            )
            http_client = httpx2.AsyncClient(auth=oauth_provider)
            await stack.enter_async_context(http_client)
        elif auth_token:
            import httpx2

            http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {auth_token}"})
            await stack.enter_async_context(http_client)
        streams = await stack.enter_async_context(streamable_http_client(url, http_client=http_client))
        read, write = streams[0], streams[1]
        session = await stack.enter_async_context(
            ClientSession(read, write, sampling_callback=handle_sampling_request)
        )
    else:
        raise ValueError(f"unknown external MCP transport {transport!r} — expected 'stdio' or 'remote'")

    await session.initialize()
    listed = await session.list_tools()
    tools = [
        # mcp 2.0.0's real field name is the snake_case `input_schema`
        # (confirmed against the installed package — older mcp releases
        # used camelCase `inputSchema`).
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema or {"type": "object", "properties": {}}}
        for t in listed.tools
    ]
    return session, tools


async def _connection_owner(name: str, kwargs: dict, ready_event: asyncio.Event, result: dict) -> None:
    """Owns one connection's AsyncExitStack for its ENTIRE lifetime, start
    to close — anyio's cancel scopes are bound to the asyncio Task that
    entered them, so the stack that opens this connection must also be
    the one that closes it (confirmed by direct live reproduction: a
    disconnect() that closed the stack from a different task raised
    "Attempted to exit cancel scope in a different task than it was
    entered in"). Runs as its own asyncio.Task (started by connect()
    below) specifically so an OAuth-enabled remote server's authorization
    wait (deep inside session.initialize(), possibly minutes long) never
    blocks connect()'s own caller — connect() only waits up to `wait_s`
    for `ready_event`, then returns, leaving this task running; a
    connection that succeeds later still registers itself here, into
    _connections, same as an immediate success would."""
    stack = AsyncExitStack()
    try:
        session, tools = await _build_session(stack, name, **kwargs)
    except Exception as exc:
        result["error"] = exc
        ready_event.set()
        await stack.aclose()
        return

    conn = _Connection(name=name, session=session, tools=tools)
    _connections[name] = conn
    _rebuild_tool_index()
    _clear_pending_oauth(name)  # the flow (if any) completed inside _build_session's session.initialize()
    logger.info("external MCP server %r: connected, %d tool(s)", name, len(tools))
    result["ok"] = True
    ready_event.set()

    await conn.close_event.wait()
    await stack.aclose()


CONNECT_WAIT_S = 10.0


async def connect(name: str, *, wait_s: float = CONNECT_WAIT_S) -> bool:
    """(Re)connects to the named, already-configured external server —
    closes any existing connection for this name first. Returns whether
    it succeeded within `wait_s`; never raises (a bad config/unreachable
    server is a normal, expected outcome here, not a bug). See
    _connection_owner's own docstring for why the real work happens in a
    background task this function only waits on, bounded — an OAuth
    authorization wait can run to OAUTH_CALLBACK_TIMEOUT_S (5 minutes),
    and a caller (an HTTP route, a Telegram command) synchronously
    awaiting that would itself hang or time out.
    oauth_authorization_url(name) is what a caller should poll/show when
    this returns False for a server that has oauth_enabled set."""
    from bot import db

    await disconnect(name)
    row = db.get_external_mcp_server(name)
    if row is None or not row["enabled"]:
        return False
    kwargs = _row_to_kwargs(row)

    ready_event = asyncio.Event()
    result: dict = {}
    task = asyncio.create_task(_connection_owner(name, kwargs, ready_event, result))
    _owner_tasks[name] = task
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=wait_s)
    except asyncio.TimeoutError:
        return False  # still connecting in the background — see this function's own docstring
    if result.get("error") is not None:
        logger.warning(
            "external MCP server %r: connection failed — its tools are unavailable this run (%s)",
            name, result["error"],
        )
        return False
    return True


async def disconnect(name: str) -> None:
    conn = _connections.pop(name, None)
    task = _owner_tasks.pop(name, None)
    if conn is not None:
        conn.close_event.set()
    elif task is not None:
        # The owner task exists but never got as far as registering a
        # _Connection (e.g. still waiting on an OAuth authorization, or
        # still spawning a slow stdio subprocess) — nothing to signal via
        # close_event yet, so cancel it directly instead.
        task.cancel()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except asyncio.CancelledError:
            pass  # expected when we just called task.cancel() above
        except Exception:
            logger.exception("external MCP server %r: error while closing connection", name)
    _clear_pending_oauth(name)
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
