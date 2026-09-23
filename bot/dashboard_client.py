"""Thin httpx wrapper over the dashboard's REST API — shared by `bot/tui/` (the Textual
TUI) and `abp_cli/` (the scriptable CLI), deliberately never importing bot.* business-logic
modules directly. This keeps exactly one implementation of validation/CRUD/lifecycle (the
dashboard/API layer, bot/dashboard/server.py) and lets either terminal surface manage a
remote/federated AgenticBotPlatform exactly like the desktop app already does, not just a
local one — and keeps the TUI and the CLI from growing two copies of the same 30-odd
one-line route wrappers.

Started life as bot/tui/client.py (which now just re-exports from here for anything that
still imports it by that name) when the CLI needed the same client and more of it: agent
settings, the full agent-config schema, and provider management, on top of the original
bots/schedules coverage.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def default_connection() -> tuple[str, str]:
    """(base_url, token) this machine's own dashboard resolves to by default — the same
    127.0.0.1:8787 + .env's DASHBOARD_TOKEN the TUI's ConnectScreen and abp_cli both start
    from. A remote/federated dashboard just needs its own host:port and token instead."""
    from bot import envfile

    return "http://127.0.0.1:8787", (envfile.get_var("DASHBOARD_TOKEN") or "")


class DashboardClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-Dashboard-Token": token},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ApiError(resp.status_code, str(detail))
        if not resp.content:
            return None
        return resp.json()

    # ---------------------------------------------------------------- bots
    async def list_bots(self) -> list[dict]:
        return await self._request("GET", "/api/bots")

    async def get_bot(self, instance_id: int) -> dict:
        return await self._request("GET", f"/api/bots/{instance_id}")

    async def create_bot(self, payload: dict) -> dict:
        return await self._request("POST", "/api/bots", json=payload)

    async def update_bot(self, instance_id: int, payload: dict) -> dict:
        return await self._request("PUT", f"/api/bots/{instance_id}", json=payload)

    async def delete_bot(self, instance_id: int) -> dict:
        return await self._request("DELETE", f"/api/bots/{instance_id}")

    async def enable_bot(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/enable")

    async def disable_bot(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/disable")

    async def start_bot(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/start")

    async def stop_bot(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/stop")

    async def restart_bot(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/restart")

    async def reset_circuit(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/circuit/reset")

    # ------------------------------------------------------------ helpers
    async def platform_guides(self) -> dict:
        return await self._request("GET", "/api/platform-guides")

    async def validate_field(self, platform: str, field: str, value: str) -> dict:
        return await self._request(
            "POST", "/api/validate-field", json={"platform": platform, "field": field, "value": value}
        )

    async def personas(self) -> list[dict]:
        return await self._request("GET", "/api/personas")

    async def models(self) -> dict:
        return await self._request("GET", "/api/models")

    # ------------------------------------------------------------ schedules
    async def list_schedules(self, instance_id: int) -> list[dict]:
        return await self._request("GET", f"/api/bots/{instance_id}/schedules")

    async def create_schedule(self, instance_id: int, payload: dict) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/schedules", json=payload)

    async def pause_schedule(self, instance_id: int, sched_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/schedules/{sched_id}/pause")

    async def resume_schedule(self, instance_id: int, sched_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/schedules/{sched_id}/resume")

    async def delete_schedule(self, instance_id: int, sched_id: int) -> dict:
        return await self._request("DELETE", f"/api/bots/{instance_id}/schedules/{sched_id}")

    # ---------------------------------------------------------------- chat
    async def send_to_bot(self, instance_id: int, text: str) -> dict:
        """POST /api/chat/send-to-bot - a real turn through the same CmdContext/
        dispatch_command/router.ask() pipeline every platform handler uses. Works for any
        bot instance regardless of its own platform (including "app" - see
        docs/agents/cli-tui.md)."""
        return await self._request("POST", "/api/chat/send-to-bot", json={"instance_id": instance_id, "text": text})

    # ------------------------------------------------------- agent settings
    async def get_agent_settings(self, instance_id: Optional[int] = None, own: bool = False) -> dict:
        params: dict[str, Any] = {"own": own}
        if instance_id is not None:
            params["instance_id"] = instance_id
        return await self._request("GET", "/api/agent-settings", params=params)

    async def set_agent_settings(self, instance_id: Optional[int], **fields) -> dict:
        return await self._request("POST", "/api/agent-settings", json={"instance_id": instance_id, **fields})

    # --------------------------------------------------------- agent config
    async def get_agent_config_schema(self) -> dict:
        return await self._request("GET", "/api/agent/config/schema")

    async def get_agent_config(self) -> dict:
        return await self._request("GET", "/api/agent/config")

    async def set_agent_config(self, changes: dict) -> dict:
        return await self._request("POST", "/api/agent/config", json={"changes": changes})

    async def reset_agent_config(self, ids: list[str]) -> dict:
        return await self._request("POST", "/api/agent/config/reset", json={"ids": ids})

    # ----------------------------------------------------------- providers
    async def list_providers(self) -> list[dict]:
        return (await self._request("GET", "/api/providers"))["providers"]

    async def set_provider(self, name: str, base_url: str, *, protocol: str = "openai",
                           api_key_env: Optional[str] = None, api_key: Optional[str] = None,
                           catalog_id: Optional[str] = None) -> dict:
        return await self._request("POST", "/api/providers", json={
            "name": name, "base_url": base_url, "protocol": protocol,
            "api_key_env": api_key_env, "api_key": api_key, "catalog_id": catalog_id,
        })

    async def delete_provider(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/providers/{name}")

    async def provider_store(self, status: Optional[str] = None) -> list[dict]:
        params = {"status": status} if status else {}
        return (await self._request("GET", "/api/providers/store", params=params))["providers"]

    async def restore_provider(self, name: str, api_key: Optional[str] = None) -> dict:
        return await self._request("POST", f"/api/providers/store/{name}/restore", json={"api_key": api_key})

    async def purge_provider(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/providers/store/{name}")

    async def provider_catalog(self) -> list[dict]:
        return (await self._request("GET", "/api/providers/catalog"))["providers"]

    async def provider_models(self, name: str, refresh: bool = False) -> list[dict]:
        return (await self._request("GET", f"/api/providers/{name}/models", params={"refresh": refresh}))["models"]

    async def toggle_provider_model(self, name: str, model_id: str, enabled: bool) -> dict:
        return await self._request("POST", f"/api/providers/{name}/models/toggle",
                                   json={"model_id": model_id, "enabled": enabled})

    async def toggle_provider_models_paid(self, name: str, enabled: bool) -> dict:
        return await self._request("POST", f"/api/providers/{name}/models/toggle-paid", json={"enabled": enabled})

    # ------------------------------------------------------------------ swarms
    async def list_swarms(self) -> list[dict]:
        return await self._request("GET", "/api/swarms")

    async def get_swarm(self, swarm_id: int) -> dict:
        return await self._request("GET", f"/api/swarms/{swarm_id}")

    async def create_swarm(self, name: str, strategy: str, config: dict, enabled: bool = True) -> dict:
        return await self._request("POST", "/api/swarms", json={"name": name, "strategy": strategy,
                                                                 "config": config, "enabled": enabled})

    async def update_swarm(self, swarm_id: int, **fields) -> dict:
        return await self._request("PUT", f"/api/swarms/{swarm_id}", json=fields)

    async def delete_swarm(self, swarm_id: int) -> dict:
        return await self._request("DELETE", f"/api/swarms/{swarm_id}")

    async def enable_swarm(self, swarm_id: int) -> dict:
        return await self._request("POST", f"/api/swarms/{swarm_id}/enable")

    async def disable_swarm(self, swarm_id: int) -> dict:
        return await self._request("POST", f"/api/swarms/{swarm_id}/disable")

    async def run_swarm(self, swarm_id: int, prompt: str, source_instance: Optional[int] = None) -> dict:
        body: dict[str, Any] = {"prompt": prompt}
        if source_instance is not None:
            body["source_instance"] = source_instance
        return await self._request("POST", f"/api/swarms/{swarm_id}/run", json=body)

    async def list_swarm_runs(self, swarm_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if swarm_id is not None:
            params["swarm_id"] = swarm_id
        return await self._request("GET", "/api/swarms/runs", params=params)

    async def get_swarm_run(self, swarm_run_id: str) -> dict:
        return await self._request("GET", f"/api/swarms/runs/{swarm_run_id}")

    async def cancel_swarm_run(self, swarm_run_id: str) -> dict:
        return await self._request("POST", f"/api/swarms/runs/{swarm_run_id}/cancel")

    # ---------------------------------------------------------------- sessions
    async def list_sessions(self, instance_id: Optional[int] = None, q: Optional[str] = None,
                            since: Optional[str] = None, until: Optional[str] = None, limit: int = 50) -> list[dict]:
        params = {k: v for k, v in dict(instance_id=instance_id, q=q, since=since, until=until, limit=limit).items()
                 if v is not None}
        return await self._request("GET", "/api/sessions", params=params)

    async def get_session(self, session_id: str) -> dict:
        return await self._request("GET", f"/api/sessions/{session_id}")

    async def delete_session(self, session_id: str) -> dict:
        return await self._request("DELETE", f"/api/sessions/{session_id}")

    async def new_bot_session(self, instance_id: int) -> dict:
        return await self._request("POST", f"/api/bots/{instance_id}/session/new")

    # ---------------------------------------------------------- terminal panel
    async def terminal_exec(self, text: str, instance_id: Optional[int] = None) -> str:
        """Runs exactly one ABP slash command (bot/commands.py's dispatcher, same one
        every platform handler uses) - not a raw shell. See docs/agents/cli-tui.md."""
        body: dict[str, Any] = {"text": text}
        if instance_id is not None:
            body["instance_id"] = instance_id
        return (await self._request("POST", "/api/terminal/exec", json=body))["output"]

    # -------------------------------------------------------------------- hooks
    async def list_hooks(self, event: Optional[str] = None) -> list[dict]:
        params = {"event": event} if event else {}
        return (await self._request("GET", "/api/hooks", params=params))["hooks"]

    async def add_hook(self, event: str, command: str, matcher: Optional[str] = None,
                       instance_id: Optional[int] = None) -> dict:
        return await self._request("POST", "/api/hooks", json={"event": event, "command": command,
                                                               "matcher": matcher, "instance_id": instance_id})

    async def enable_hook(self, hook_id: int) -> dict:
        return await self._request("POST", f"/api/hooks/{hook_id}/enable")

    async def disable_hook(self, hook_id: int) -> dict:
        return await self._request("POST", f"/api/hooks/{hook_id}/disable")

    async def delete_hook(self, hook_id: int) -> dict:
        return await self._request("DELETE", f"/api/hooks/{hook_id}")

    # ----------------------------------------------------------------- plugins
    async def list_plugins(self) -> list[dict]:
        return (await self._request("GET", "/api/plugins"))["plugins"]

    async def install_plugin(self, path: str) -> dict:
        return await self._request("POST", "/api/plugins", json={"path": path})

    async def create_plugin(self, name: str, code: str) -> dict:
        return await self._request("POST", "/api/plugins/create", json={"name": name, "code": code})

    async def enable_plugin(self, name: str) -> dict:
        return await self._request("POST", f"/api/plugins/{name}/enable")

    async def disable_plugin(self, name: str) -> dict:
        return await self._request("POST", f"/api/plugins/{name}/disable")

    async def delete_plugin(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/plugins/{name}")

    # ------------------------------------------------------------------ skills
    async def list_skills(self, instance_id: Optional[int] = None) -> list[dict]:
        params = {"instance_id": instance_id} if instance_id is not None else {}
        return (await self._request("GET", "/api/skills", params=params))["skills"]

    async def create_skill(self, instance_id: Optional[int], name: str, description: str, content: str,
                           is_global: bool = False) -> dict:
        return await self._request("POST", "/api/skills", json={"instance_id": instance_id, "name": name,
                                                                "description": description, "content": content,
                                                                "global": is_global})

    async def delete_skill(self, name: str, instance_id: Optional[int] = None) -> dict:
        params = {"instance_id": instance_id} if instance_id is not None else {}
        return await self._request("DELETE", f"/api/skills/{name}", params=params)

    async def skill_packs(self) -> list[dict]:
        return (await self._request("GET", "/api/skills/packs"))["packs"]

    async def fetch_skill_pack(self, url: str, ref: Optional[str] = None, subdir: Optional[str] = None) -> dict:
        return await self._request("POST", "/api/skills/fetch", json={"url": url, "ref": ref, "subdir": subdir})

    async def skill_quarantine(self) -> list[dict]:
        return (await self._request("GET", "/api/skills/quarantine"))["packs"]

    async def review_skill_quarantine(self, name: str, decision: str) -> dict:
        route = "approve" if decision == "approve" else "reject"
        return await self._request("POST", f"/api/skills/quarantine/{route}", json={"name": name})

    async def skill_drafts(self) -> list[dict]:
        return (await self._request("GET", "/api/skills/drafts"))["drafts"]

    async def review_skill_draft(self, name: str, decision: str) -> dict:
        route = "approve" if decision == "approve" else "reject"
        return await self._request("POST", f"/api/skills/drafts/{route}", json={"name": name})

    # ---------------------------------------------------------------------- mcp
    async def list_mcp_servers(self) -> list[dict]:
        return await self._request("GET", "/api/mcp")

    async def mcp_server_logs(self, name: str, lines: int = 50) -> list[str]:
        return (await self._request("GET", f"/api/mcp/{name}/logs", params={"lines": lines}))["lines"]

    async def enable_mcp_server(self, name: str) -> dict:
        return await self._request("POST", f"/api/mcp/{name}/enable")

    async def disable_mcp_server(self, name: str) -> dict:
        return await self._request("POST", f"/api/mcp/{name}/disable")

    async def mcp_pins(self) -> list[dict]:
        return (await self._request("GET", "/api/mcp/pins"))["tools"]

    async def approve_mcp_pin(self, server: str, tool: str) -> dict:
        return await self._request("POST", "/api/mcp/pins/approve", json={"server": server, "tool": tool})

    async def list_external_mcp_servers(self, instance_id: Optional[int] = None) -> list[dict]:
        params = {"instance_id": instance_id} if instance_id is not None else {}
        return (await self._request("GET", "/api/mcp-external", params=params))["servers"]

    async def add_external_mcp_server(self, name: str, transport: str, *, command: Optional[str] = None,
                                      args: Optional[list] = None, env: Optional[dict] = None,
                                      url: Optional[str] = None, auth_token: Optional[str] = None,
                                      oauth_enabled: bool = False, instance_id: Optional[int] = None) -> dict:
        return await self._request("POST", "/api/mcp-external", json={
            "name": name, "transport": transport, "command": command, "args": args, "env": env,
            "url": url, "auth_token": auth_token, "oauth_enabled": oauth_enabled, "instance_id": instance_id,
        })

    async def enable_external_mcp_server(self, name: str) -> dict:
        return await self._request("POST", f"/api/mcp-external/{name}/enable")

    async def disable_external_mcp_server(self, name: str) -> dict:
        return await self._request("POST", f"/api/mcp-external/{name}/disable")

    async def remove_external_mcp_server(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/mcp-external/{name}")

    # --------------------------------------------------------- security & devices
    async def list_allowed_users(self) -> list[dict]:
        return await self._request("GET", "/api/security/allowed-users")

    async def add_allowed_user(self, telegram_id: str, name: Optional[str] = None) -> dict:
        params = {"name": name} if name else {}
        return await self._request("POST", f"/api/security/allowed-users/{telegram_id}", params=params)

    async def remove_allowed_user(self, telegram_id: str) -> dict:
        return await self._request("DELETE", f"/api/security/allowed-users/{telegram_id}")

    async def get_permissions(self) -> dict:
        return await self._request("GET", "/api/agent/permissions")

    async def validate_permission_rules(self, rules: list) -> dict:
        return await self._request("POST", "/api/agent/permissions/validate", json={"rules": rules})

    async def get_instance_permissions(self, instance_id: int) -> dict:
        return await self._request("GET", f"/api/instances/{instance_id}/permissions")

    async def set_instance_permissions(self, instance_id: int, mode: Optional[str] = None,
                                       rules: Optional[list] = None) -> dict:
        body: dict[str, Any] = {}
        if mode is not None:
            body["mode"] = mode
        if rules is not None:
            body["rules"] = rules
        return await self._request("PUT", f"/api/instances/{instance_id}/permissions", json=body)

    async def create_mobile_key(self, label: str, tier: str, host: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"label": label, "tier": tier}
        if host:
            body["host"] = host
        return await self._request("POST", "/api/mobile-keys", json=body)

    async def list_mobile_keys(self) -> list[dict]:
        return await self._request("GET", "/api/mobile-keys")

    async def delete_mobile_key(self, key_id: int) -> dict:
        return await self._request("DELETE", f"/api/mobile-keys/{key_id}")

    async def set_mobile_key_tier(self, key_id: int, tier: str) -> dict:
        return await self._request("POST", f"/api/mobile-keys/{key_id}/tier", json={"tier": tier})

    async def list_devices(self) -> list[dict]:
        return await self._request("GET", "/api/devices")

    # --------------------------------------------------- snapshots/env/config/diagnostics
    async def list_snapshots(self) -> list[dict]:
        return (await self._request("GET", "/api/snapshots"))["snapshots"]

    async def create_snapshot(self, label: Optional[str] = None) -> dict:
        return await self._request("POST", "/api/snapshots", json={"label": label} if label else {})

    async def restore_snapshot(self, name: str) -> dict:
        return await self._request("POST", f"/api/snapshots/{name}/restore")

    async def delete_snapshot(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/snapshots/{name}")

    async def env_status(self) -> dict:
        return await self._request("GET", "/api/env")

    async def get_config(self) -> dict:
        return await self._request("GET", "/api/config")

    async def reload_config(self) -> dict:
        return await self._request("POST", "/api/config/reload")

    async def set_config_path(self, path: list, value: Any) -> dict:
        return await self._request("POST", "/api/config/set", json={"path": path, "value": value})

    async def diagnostics_summary(self) -> dict:
        return await self._request("GET", "/api/diagnostics/summary")

    async def crash_reports(self, limit: int = 50) -> list[dict]:
        return (await self._request("GET", "/api/diagnostics/crash-reports", params={"limit": limit}))["reports"]

    # ------------------------------------------------------------------- peers
    async def list_peers(self) -> list[dict]:
        return await self._request("GET", "/api/peers")

    async def peer_self_address(self) -> dict:
        return await self._request("GET", "/api/peers/self-address")

    async def create_peer_pairing_token(self, base_url: Optional[str] = None) -> dict:
        return await self._request("POST", "/api/peers/pairing-token", json={"base_url": base_url} if base_url else {})

    async def link_peer(self, name: str, pairing_token: str, my_base_url: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"name": name, "pairing_token": pairing_token}
        if my_base_url:
            body["my_base_url"] = my_base_url
        return await self._request("POST", "/api/peers/link", json=body)

    async def remove_peer(self, peer_id: int) -> dict:
        return await self._request("DELETE", f"/api/peers/{peer_id}")

    async def peer_overview(self, peer_id: int) -> dict:
        return await self._request("GET", f"/api/peers/{peer_id}/overview")

    async def peer_bots(self, peer_id: int) -> list[dict]:
        return await self._request("GET", f"/api/peers/{peer_id}/bots")

    # ------------------------------------------------------------------ kanban
    async def kanban_boards(self, instance_id: int) -> list[dict]:
        return (await self._request("GET", "/api/kanban/boards", params={"instance_id": instance_id}))["boards"]

    async def kanban_cards(self, instance_id: int, board: str = "default") -> list[dict]:
        return (await self._request("GET", "/api/kanban/cards",
                                    params={"board": board, "instance_id": instance_id}))["cards"]

    async def add_kanban_card(self, instance_id: int, text: str, board: str = "default", column: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"instance_id": instance_id, "text": text, "board": board}
        if column:
            body["column"] = column
        return await self._request("POST", "/api/kanban/cards", json=body)

    async def move_kanban_card(self, card_id: int, instance_id: int, column: str) -> dict:
        return await self._request("POST", f"/api/kanban/cards/{card_id}/move",
                                   json={"instance_id": instance_id, "column": column})

    async def delete_kanban_card(self, card_id: int, instance_id: int) -> dict:
        return await self._request("DELETE", f"/api/kanban/cards/{card_id}", params={"instance_id": instance_id})

    # ------------------------------------------------------------ ssh toolkit
    async def ssh_toolkit_status(self) -> dict:
        return await self._request("GET", "/api/ssh-toolkit/status")

    async def ssh_toolkit_connections(self) -> list[dict]:
        return (await self._request("GET", "/api/ssh-toolkit/connections"))["connections"]

    async def ssh_toolkit_get_connection(self, name: str) -> dict:
        return await self._request("GET", f"/api/ssh-toolkit/connections/{name}")

    async def ssh_toolkit_add_connection(self, name: str, host_name: str, *, port: int = 22,
                                         user: Optional[str] = None, identity_file: Optional[str] = None,
                                         generate_key: bool = False, proxy_jump: Optional[str] = None,
                                         tags: Optional[str] = None, multiplex: bool = False,
                                         force: bool = False) -> dict:
        return await self._request("POST", "/api/ssh-toolkit/connections", json={
            "name": name, "host_name": host_name, "port": port, "user": user,
            "identity_file": identity_file, "generate_key": generate_key, "proxy_jump": proxy_jump,
            "tags": tags, "multiplex": multiplex, "force": force,
        })

    async def ssh_toolkit_remove_connection(self, name: str) -> dict:
        return await self._request("DELETE", f"/api/ssh-toolkit/connections/{name}")

    async def ssh_toolkit_test_connection(self, name: str) -> dict:
        return await self._request("POST", f"/api/ssh-toolkit/connections/{name}/test")

    async def ssh_toolkit_run(self, name: str, command: str) -> str:
        return (await self._request("POST", f"/api/ssh-toolkit/connections/{name}/run", json={"command": command}))["output"]

    async def ssh_toolkit_status_all(self) -> list[dict]:
        return (await self._request("GET", "/api/ssh-toolkit/status-all"))["connections"]

    async def ssh_toolkit_graph(self) -> list[dict]:
        return (await self._request("GET", "/api/ssh-toolkit/graph"))["nodes"]

    async def ssh_toolkit_check_update(self) -> dict:
        return await self._request("GET", "/api/ssh-toolkit/update/check")

    async def ssh_toolkit_apply_update(self) -> dict:
        return await self._request("POST", "/api/ssh-toolkit/update/apply")

    async def ssh_toolkit_get_auto_update(self) -> dict:
        return await self._request("GET", "/api/ssh-toolkit/auto-update")

    async def ssh_toolkit_set_auto_update(self, mode: str) -> dict:
        return await self._request("POST", "/api/ssh-toolkit/auto-update", json={"mode": mode})

    # ------------------------------------------- ssh toolkit session monitor
    async def ssh_session_start(self, name: str, command: str) -> str:
        return (await self._request("POST", "/api/ssh-toolkit/session/start", json={"name": name, "command": command}))["session_id"]

    async def ssh_session_list(self) -> list[dict]:
        return (await self._request("GET", "/api/ssh-toolkit/session"))["sessions"]

    async def ssh_session_stop(self, session_id: str) -> dict:
        return await self._request("POST", f"/api/ssh-toolkit/session/{session_id}/stop")

    async def ssh_session_record_start(self, session_id: str) -> int:
        return (await self._request("POST", f"/api/ssh-toolkit/session/{session_id}/record/start"))["recording_id"]

    async def ssh_session_record_pause(self, session_id: str) -> dict:
        return await self._request("POST", f"/api/ssh-toolkit/session/{session_id}/record/pause")

    async def ssh_session_record_resume(self, session_id: str) -> dict:
        return await self._request("POST", f"/api/ssh-toolkit/session/{session_id}/record/resume")

    async def ssh_session_record_stop(self, session_id: str) -> Optional[int]:
        return (await self._request("POST", f"/api/ssh-toolkit/session/{session_id}/record/stop"))["recording_id"]

    async def ssh_recordings_list(self) -> list[dict]:
        return (await self._request("GET", "/api/ssh-toolkit/recordings"))["recordings"]

    async def ssh_recording_get(self, recording_id: int) -> dict:
        return await self._request("GET", f"/api/ssh-toolkit/recordings/{recording_id}")

    async def ssh_recording_delete(self, recording_id: int) -> dict:
        return await self._request("DELETE", f"/api/ssh-toolkit/recordings/{recording_id}")
