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
