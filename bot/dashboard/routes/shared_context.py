"""Dashboard routes: shared context.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI, HTTPException


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # Small, named markdown docs any registered instance (any backend) can
    # read/write via the read_project_context/write_project_context tools
    # or these same routes — see bot/shared_context.py's module docstring.

    @app.get("/api/context", dependencies=[Depends(_require_token_or_api_key)])
    async def api_context_list():
        from bot import shared_context

        return {"docs": shared_context.list_docs()}

    @app.get("/api/context/{name}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_context_get(name: str):
        from bot import shared_context

        try:
            doc = shared_context.read_doc(name)
        except shared_context.SharedContextError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if doc is None:
            raise HTTPException(status_code=404, detail=f"no shared context doc named {name!r}")
        return doc

    @app.post("/api/context/{name}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_context_set(name: str, payload: dict = Body(...)):
        from bot import shared_context

        content = payload.get("content")
        if content is None:
            raise HTTPException(status_code=400, detail="payload must include 'content'")
        actor = payload.get("actor") or "dashboard"
        try:
            doc = shared_context.write_doc(name, content, actor)
        except shared_context.SharedContextError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "doc": doc}

    @app.delete("/api/context/{name}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_context_delete(name: str):
        from bot import shared_context

        try:
            removed = shared_context.delete_doc(name)
        except shared_context.SharedContextError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "removed": removed}
