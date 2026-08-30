"""Thin httpx wrapper over the dashboard's REST API — the TUI's only way
of talking to BotServer, deliberately never importing bot.* business-logic
modules directly. This keeps exactly one implementation of validation/
CRUD/lifecycle (the dashboard/API layer, bot/dashboard/server.py) and lets
the TUI manage a remote/federated BotServer exactly like the desktop app
already does, not just a local one.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


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
