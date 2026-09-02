"""Exposes this app's own dashboard control surface as an MCP server.

Everything the dashboard GUI can do — check status, list jobs, start/stop
Claude Desktop, flip a backend override, toggle an MCP server on/off — is
already a REST call on the running dashboard API (bot/dashboard/server.py).
This module doesn't reimplement any of that; it's a thin stdio MCP server
whose tools proxy those same endpoints over HTTP to 127.0.0.1, using the
same DASHBOARD_TOKEN the GUI uses. That keeps one source of truth: a tool
added to the dashboard API shows up here by adding one proxy function, not
by re-deriving business logic.

Every tool call shares one long-lived httpx.AsyncClient (connection pooling
across calls instead of a fresh TCP handshake each time), retries briefly
on connection-level failures (the dashboard mid hot-reload, or this
process racing the dashboard's own startup — never on an actual HTTP error
response, which is authoritative), and logs to logs/mcp_server.log — the
only place it safely can, since stdout is the MCP/JSON-RPC transport
itself and would be corrupted by any print/console logging.

Run standalone for testing:
    python -m bot.mcp_server

To use from Claude Desktop or Claude Code, register it as an MCP server
pointing at this project's venv python running `-m bot.mcp_server` — see
desktop.py's register_self_mcp() for the Claude Desktop case, or run
`claude mcp add bot-server -- <path to .venv>\\Scripts\\python.exe -m bot.mcp_server`
(Windows) or `claude mcp add bot-server -- <path to .venv>/bin/python -m bot.mcp_server`
(Linux/macOS) for Claude Code. Either way the dashboard API itself must
already be running (launch the built app, or `python -m bot.main`) — this
process is a thin client, not a second copy of the server.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from typing import Any, Optional

import httpx
from mcp.server.mcpserver import MCPServer

from bot import envfile

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = os.environ.get("DASHBOARD_PORT", "8787")
BASE_URL = f"http://{HOST}:{PORT}"

# stdio *is* the MCP transport here — anything written to stdout would
# corrupt the JSON-RPC stream, so logging can only ever go to a file, never
# a console handler. Shares the same logs/ directory bot/main.py's own
# rotating log lives in, just a different file, since this runs as a
# separate process (spawned by Claude Desktop/Code, not by bot/main.py).
_LOG_DIR = envfile.PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "mcp_server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot.mcp_server")

mcp = MCPServer("bot-server")

# A single long-lived client instead of one per tool call: this process
# lives for the whole Claude Desktop/Code session, so paying connection
# setup cost on every single tool invocation was pure overhead — httpx
# already pools/reuses the underlying TCP connection across requests made
# on the same client. Created lazily (not at import time) since MCPServer
# tools all run inside an event loop that doesn't exist yet at import.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE_URL)
    return _client


# A single failed request to the dashboard (it's mid hot-reload, or this
# tool call raced the dashboard's own startup) shouldn't surface as a hard
# tool failure — retried only for connection-level failures, never an
# actual HTTP error response (the dashboard answering with a real 4xx/5xx
# is authoritative, not worth retrying). Mirrors the same pattern already
# used for peer-server linking (bot/peers.py) and the desktop updater.
_RETRY_DELAYS_S = (0.5, 1.5)


def _token() -> Optional[str]:
    # Prefer a real process env var (e.g. set by desktop.py's self-registered
    # MCP entry) over reading .env fresh each call — either way, falls back
    # to whatever the running dashboard actually loaded at its own startup.
    return os.environ.get("DASHBOARD_TOKEN") or envfile.get_var("DASHBOARD_TOKEN")


async def _request(method: str, path: str, timeout: float = 15.0, **kwargs: Any) -> Any:
    headers = kwargs.pop("headers", {})
    token = _token()
    if token:
        headers["X-Dashboard-Token"] = token
    client = _get_client()

    resp = None
    last_exc: Optional[Exception] = None
    for delay in (0.0, *_RETRY_DELAYS_S):
        if delay:
            logger.warning("retrying %s %s in %.1fs after a connection error: %s", method, path, delay, last_exc)
            await asyncio.sleep(delay)
        try:
            resp = await client.request(method, path, headers=headers, timeout=timeout, **kwargs)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
    if resp is None:
        logger.error("giving up on %s %s: %s", method, path, last_exc)
        return {"error": f"couldn't reach the dashboard at {BASE_URL} — is it running? ({last_exc})"}

    if resp.status_code == 401:
        logger.warning("%s %s -> 401", method, path)
        return {"error": "invalid or missing DASHBOARD_TOKEN — check .env"}
    if resp.status_code == 503:
        logger.warning("%s %s -> 503", method, path)
        return {"error": "dashboard reports DASHBOARD_TOKEN is not set yet — run the setup wizard first"}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        logger.warning("%s %s -> %s: %s", method, path, resp.status_code, detail)
        return {"error": str(detail)}
    logger.info("%s %s -> %s", method, path, resp.status_code)
    return resp.json()


@mcp.tool()
async def get_status() -> dict:
    """Overview snapshot: job counts, desktop process state, config version, default backend."""
    return await _request("GET", "/api/overview")


@mcp.tool()
async def list_jobs(limit: int = 20, status: Optional[str] = None) -> list:
    """List recent router jobs (each /ask attempt), newest first."""
    params = {"limit": limit}
    if status:
        params["status"] = status
    return await _request("GET", "/api/jobs", params=params)


@mcp.tool()
async def get_config() -> dict:
    """Current config/backends.yaml contents plus its hot-reload version."""
    return await _request("GET", "/api/config")


@mcp.tool()
async def set_backend(action_or_default: str, backend: str) -> dict:
    """Set the backend for one action type (or 'default') to any of
    'api', 'cli', 'ui', 'hermes_cli', 'hermes_gateway', or 'custom_model'.
    Claude/Hermes each keep their own 'default' slot under the hood — see
    the dashboard's own segmented Claude/Hermes default pickers — this
    tool routes to whichever slot matches the backend family
    automatically."""
    return await _request("POST", f"/api/backend/{action_or_default}/{backend}")


@mcp.tool()
async def reload_config() -> dict:
    """Force config/backends.yaml to re-read from disk immediately."""
    return await _request("POST", "/api/config/reload")


@mcp.tool()
async def list_mcp_servers() -> list:
    """List MCP servers configured in Claude Desktop's own claude_desktop_config.json, with enabled/disabled state."""
    return await _request("GET", "/api/mcp")


@mcp.tool()
async def enable_mcp_server(name: str) -> dict:
    """Enable a Claude-Desktop-configured MCP server by name."""
    return await _request("POST", f"/api/mcp/{name}/enable")


@mcp.tool()
async def disable_mcp_server(name: str) -> dict:
    """Disable a Claude-Desktop-configured MCP server by name."""
    return await _request("POST", f"/api/mcp/{name}/disable")


@mcp.tool()
async def start_claude_desktop() -> dict:
    """Launch Claude Desktop if it isn't already running."""
    return await _request("POST", "/api/desktop/start")


@mcp.tool()
async def stop_claude_desktop() -> dict:
    """Terminate the running Claude Desktop process."""
    return await _request("POST", "/api/desktop/stop")


@mcp.tool()
async def restart_claude_desktop() -> dict:
    """Stop then relaunch Claude Desktop."""
    return await _request("POST", "/api/desktop/restart")


@mcp.tool()
async def get_setup_status() -> dict:
    """Which required .env fields (Telegram token, Anthropic key, allowed user IDs, dashboard token) are present and valid."""
    return await _request("GET", "/api/setup/status")


@mcp.tool()
async def create_snapshot(label: Optional[str] = None) -> dict:
    """Take a point-in-time snapshot of BotServer's own config (backends.yaml,
    providers.yaml) and database — safe to call at any time, never stops
    or interrupts the running app. Call this BEFORE making a risky change
    to BotServer's own code/config/data (a migration, a bulk edit, an
    experimental refactor) so restore_snapshot can undo it if something
    goes wrong. `label` is an optional short tag (e.g. "before-db-migration")
    to make the snapshot easier to find in list_snapshots later."""
    return await _request("POST", "/api/snapshots", json={"label": label} if label else {})


@mcp.tool()
async def list_snapshots() -> dict:
    """List every snapshot taken so far, newest first, with name/label/size."""
    return await _request("GET", "/api/snapshots")


@mcp.tool()
async def restore_snapshot(name: str) -> dict:
    """Restore config and database from a previous snapshot (see
    list_snapshots for valid names) — reverts BOTH files and data rows to
    that point in time. Briefly closes and reopens the database connection
    to swap the file safely; does not restart the app."""
    return await _request("POST", f"/api/snapshots/{name}/restore")


@mcp.tool()
async def hot_reload_status() -> dict:
    """Current hot-reload status: whether it's enabled, whether it's
    degraded (a previous reload failed partway through and a full
    process restart is now required — see restore_snapshot/a manual
    restart, not this), and the most recent reload events."""
    return await _request("GET", "/api/hotreload/status")


@mcp.tool()
async def trigger_hot_reload() -> dict:
    """Force a full hot-reload cycle right now over every hot-reloadable
    bot/*.py module, regardless of whether anything actually changed on
    disk — useful right after editing BotServer's own code to confirm it
    applied instead of waiting for the file watcher. Does nothing (and
    reports "restart_required"/"degraded") if the change touched a file
    that can't be safely hot-reloaded — see bot/hotreload.py's module
    docstring for exactly which files those are and why."""
    return await _request("POST", "/api/hotreload/run")


@mcp.tool()
async def ask_instance(source_instance: str, target_instance: str, prompt: str) -> dict:
    """Ask another registered bot instance (by its bot_instances name, see
    get_status/the Bots tab) a one-off question and get its reply back.

    source_instance is YOUR OWN persona's bot_instances name — state which
    one you are. This is self-declared, not verified server-side; it only
    drives the agent_control allowlist (if enabled) and the audit log, not
    a security boundary on its own. Subject to the dashboard's
    trust_all/allowlist agent_control setting — a denied target returns a
    clear error, not a stack trace."""
    return await _request("POST", "/api/agent/ask", timeout=90.0, json={
        "source_instance": source_instance, "target_instance": target_instance, "prompt": prompt,
    })


@mcp.tool()
async def create_bot_instance(
    name: str,
    platform: str,
    backend: str,
    credentials: Optional[dict] = None,
    allowed_user_ids: Optional[list] = None,
    model: Optional[str] = None,
    persona: Optional[str] = None,
    can_target: Optional[list] = None,
    enabled: bool = True,
    hermes_home: Optional[str] = None,
) -> dict:
    """Create a new bot instance — e.g. stand up a fresh Hermes-backed
    worker on the fly rather than requiring the dashboard's Bots tab.
    `backend` is any of 'api'/'cli'/'ui'/'hermes_cli'/'hermes_gateway'/
    'custom_model'. `model` for a hermes_gateway instance is a bare model
    id (see list_available_models); for custom_model it must be
    '<provider>/<model_id>'. `can_target` (list of other instance ids)
    matters only when agent_control.mode is 'allowlist' — see get_config.
    `credentials`/`allowed_user_ids` may be omitted for a Hermes worker
    with no direct platform connection of its own (it's only ever reached
    via ask_instance/dispatch_swarm_goal, never a live chat platform) —
    pass platform='telegram' with empty credentials/allowed_user_ids in
    that case; it simply won't start a live connection until configured.
    `hermes_home` (hermes_gateway only) gives this instance its own
    isolated Hermes runtime (own config.yaml/sessions/cwd/port) instead of
    sharing the one machine-wide default every other hermes_gateway
    instance uses — pass a dedicated directory path (e.g.
    'C:\\hermes-homes\\worker1' or '~/.hermes-worker1') to actually get
    independent agents, not just independent bot_instances rows pointed
    at the same shared Hermes state."""
    return await _request("POST", "/api/bots", json={
        "name": name, "platform": platform, "backend": backend,
        "credentials": credentials or {}, "allowed_user_ids": allowed_user_ids or [],
        "model": model, "persona": persona, "can_target": can_target or [],
        "enabled": enabled, "hermes_home": hermes_home,
    })


@mcp.tool()
async def delete_bot_instance(instance_id: int) -> dict:
    """Stop and permanently delete a bot instance by id (see get_status/
    the Bots tab for ids). Irreversible — the instance's credentials,
    session links, and history are gone; jobs/audit-log rows referencing
    it are kept for history."""
    return await _request("DELETE", f"/api/bots/{instance_id}")


@mcp.tool()
async def list_available_models(instance_id: Optional[int] = None) -> dict:
    """The full model catalog Claude needs to pick an "optimal free model":
    Claude's own live /v1/models, BotServer's custom_model provider
    registry's live catalogs, and (pass instance_id for a specific
    hermes_gateway-backed bot instance) that instance's own live,
    pricing-annotated Hermes inventory — real `free: bool` per model, not
    a naming-convention guess. The response's `pricing_source` tells you
    whether that data is "live" (trust it), "cache_fallback" (Hermes's
    stale disk cache — id-suffix heuristic only), or "unavailable"
    (nothing reachable — omit instance_id or check the instance is
    actually hermes_gateway-backed and its gateway can start)."""
    params = {"instance_id": instance_id} if instance_id is not None else {}
    return await _request("GET", "/api/models", params=params)


@mcp.tool()
async def configure_delegation(
    instance_id: int,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_concurrent_children: Optional[int] = None,
    max_spawn_depth: Optional[int] = None,
    subagent_auto_approve: Optional[bool] = None,
    confirm: bool = False,
) -> dict:
    """Sets defaults in a hermes_gateway-backed instance's own Hermes
    delegation config — which provider/model delegate_task's children run
    on (e.g. a free model while the parent stays on something stronger),
    how many children can run in parallel, how deep orchestrator-role
    recursion may go, and whether children may run dangerous shell
    commands with no human approval.

    IMPORTANT LIMITATION: until every Hermes instance gets its own
    isolated HERMES_HOME, this writes to the ONE Hermes config every
    hermes_gateway-backed instance on this machine shares — it affects
    all of them, not just `instance_id`.

    subagent_auto_approve=true is a real safety-relevant switch (children
    run shell/file-write commands with zero human approval) — pass
    confirm=true to acknowledge that explicitly, or the call is refused."""
    if subagent_auto_approve is True and not confirm:
        return {"error": "subagent_auto_approve=true requires confirm=true — see this tool's own docstring for why"}
    return await _request("POST", f"/api/hermes/{instance_id}/delegation", json={
        "provider": provider, "model": model,
        "max_concurrent_children": max_concurrent_children, "max_spawn_depth": max_spawn_depth,
        "subagent_auto_approve": subagent_auto_approve, "confirm": confirm,
    })


@mcp.tool()
async def dispatch_swarm_goal(
    instance_id: int,
    goal: str,
    worker_provider: Optional[str] = None,
    worker_model: Optional[str] = None,
    max_children: Optional[int] = None,
) -> dict:
    """The actual "use Hermes Agents with the optimal free models to work
    on this" lever: configures this hermes_gateway-backed instance's
    delegation defaults (auto-picking the cheapest currently-free model
    from its own live inventory if worker_provider/worker_model aren't
    given) then sends `goal` as a prompt instructing Hermes's own agent to
    decompose it and fan it out via its delegate_task tool, waiting for
    the synthesized final answer.

    delegate_task cannot be invoked directly over MCP (it needs a live
    Hermes agent-loop context) — this tool's real leverage is the config
    + prompt it sends, trusting Hermes's own agent to do the delegating.
    There is no external steer/stop for an individual sub-agent spawned
    this way; cancelling only stops BotServer's own wait, not the
    underlying Hermes task (see the Hermes-swarm plan's Phase 3 notes).
    This call blocks until the whole swarm run (parent + every child)
    finishes — call list_available_models first if you want to see what
    "optimal free" actually resolved to before committing to a run."""
    return await _request("POST", f"/api/hermes/{instance_id}/dispatch", timeout=900.0, json={
        "goal": goal, "worker_provider": worker_provider, "worker_model": worker_model,
        "max_children": max_children,
    })


@mcp.tool()
async def run_swarm(source_instance: str, swarm: str, prompt: str) -> dict:
    """Trigger a multi-step swarm run (see the dashboard's Swarms tab) as the
    given source persona, subject to the same agent_control allowlist as
    ask_instance — every bot instance the swarm's config references must be
    a permitted target of source_instance when allowlist mode is on.
    `swarm` is the swarm's name or id."""
    swarms = await _request("GET", "/api/swarms")
    if isinstance(swarms, dict) and "error" in swarms:
        return swarms
    match = next((s for s in swarms if str(s.get("id")) == str(swarm) or s.get("name") == swarm), None)
    if match is None:
        return {"error": f"swarm {swarm!r} not found"}
    return await _request("POST", f"/api/swarms/{match['id']}/run", timeout=30.0, json={
        "source_instance": source_instance, "prompt": prompt,
    })


if __name__ == "__main__":
    logger.info("bot-server MCP server starting (dashboard at %s)", BASE_URL)
    try:
        mcp.run(transport="stdio")
    finally:
        if _client is not None:
            try:
                # mcp.run() already ran (and closed) its own event loop, so
                # this has to spin up a fresh one just to close the client's
                # sockets — which on Windows' Proactor loop reliably raises
                # "Event loop is closed" from libuv/IOCP cleanup tied to the
                # now-gone original loop. Harmless at process-exit time (the
                # OS reclaims the sockets regardless) and not worth a scary
                # traceback every time Claude Desktop/Code spawns and kills
                # this short-lived process, so it's swallowed here rather
                # than fixed "properly" — there is no clean fix for a
                # cross-event-loop close on this platform.
                asyncio.run(_client.aclose())
            except Exception:
                pass
        logger.info("bot-server MCP server exiting")
