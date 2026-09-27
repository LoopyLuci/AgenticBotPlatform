"""Dashboard routes: pairing.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException

from bot import pairing


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # Approving/denying a pending chat-platform pairing request — see
    # bot/pairing.py. Listing is scoped to one instance when instance_id is
    # given, otherwise every pending request across every bot (for a
    # dashboard-wide "Pending Pairings" panel).

    @app.get("/api/pairing", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_list(instance_id: Optional[int] = None):
        return {"pending": pairing.list_pending(instance_id)}

    @app.post("/api/pairing/{pairing_id}/approve", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_approve(pairing_id: int):
        try:
            row = pairing.approve(pairing_id, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "pairing": row}

    @app.post("/api/pairing/{pairing_id}/deny", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_deny(pairing_id: int):
        try:
            row = pairing.deny(pairing_id, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "pairing": row}
