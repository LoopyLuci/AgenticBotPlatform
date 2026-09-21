"""Approvals API (roadmap P6): what the agent wants to do, with a preview, and a way to decide from any surface.

    GET  /api/approvals?status=pending|all&instance_id=   reviewable approvals, newest first
    GET  /api/approvals/{id}                              one, with its diff / command preview
    POST /api/approvals/{id}/resolve   {"outcome": "once|session|always|deny"}

Reading follows the dashboard's normal auth (the desktop token or a paired device's key), so a paired phone can see
and answer approvals. Answering "always" or "session" (a standing grant) needs the dashboard token itself: a
paired device may approve this one call, but may not widen what the agent is allowed to do from now on."""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel


class _Resolve(BaseModel):
    outcome: str


def register(app: FastAPI, read_auth: Callable, write_auth: Callable, caller_is_owner: Callable) -> None:
    from bot import approvals_view

    read = [Depends(read_auth)]

    @app.get("/api/approvals", dependencies=read)
    async def list_approvals(status: str = Query("pending", pattern="^(pending|all|approved_once|approved_session|approved_always|denied|expired)$"),
                             instance_id: Optional[int] = None, limit: int = Query(50, ge=1, le=200)):
        return {"approvals": approvals_view.listing(status=status, instance_id=instance_id, limit=limit)}

    @app.get("/api/approvals/{approval_id}", dependencies=read)
    async def get_approval(approval_id: int):
        item = approvals_view.get(approval_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such approval")
        return item

    @app.post("/api/approvals/{approval_id}/resolve", dependencies=read)
    async def resolve_approval(approval_id: int, body: _Resolve, owner: bool = Depends(caller_is_owner)):
        if body.outcome not in approvals_view.OUTCOMES:
            raise HTTPException(status_code=400, detail=f"outcome must be one of {', '.join(approvals_view.OUTCOMES)}")
        if body.outcome in ("session", "always") and not owner:
            raise HTTPException(status_code=403, detail="only the dashboard token can grant standing approvals; approve this call once instead")
        result = approvals_view.decide(approval_id, body.outcome, actor="dashboard" if owner else "device")
        if result == "not_found":
            raise HTTPException(status_code=404, detail="no such approval")
        if result == "already_resolved":
            raise HTTPException(status_code=409, detail="already resolved (or the run that asked has ended)")
        return {"id": approval_id, "outcome": body.outcome}
