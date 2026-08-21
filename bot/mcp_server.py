"""Exposes this app's own dashboard control surface as an MCP server.

Everything the dashboard GUI can do — check status, list jobs, start/stop
Claude Desktop, flip a backend override, toggle an MCP server on/off — is
already a REST call on the running dashboard API (bot/dashboard/server.py).
This module doesn't reimplement any of that; it's a thin stdio MCP server
whose tools proxy those same endpoints over HTTP to 127.0.0.1, using the
same DASHBOARD_TOKEN the GUI uses. That keeps one source of truth: a tool
added to the dashboard API shows up here by adding one proxy function, not
by re-deriving business logic.

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

import os
from typing import Any, Optional

import httpx
from mcp.server.mcpserver import MCPServer

from bot import envfile

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = os.environ.get("DASHBOARD_PORT", "8787")
BASE_URL = f"http://{HOST}:{PORT}"

mcp = MCPServer("bot-server")


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
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            return {"error": "invalid or missing DASHBOARD_TOKEN — check .env"}
        if resp.status_code == 503:
            return {"error": "dashboard reports DASHBOARD_TOKEN is not set yet — run the setup wizard first"}
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return {"error": str(detail)}
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
    """Set the backend for one action type (or 'default') to 'api', 'cli', or 'ui'."""
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
    mcp.run(transport="stdio")
