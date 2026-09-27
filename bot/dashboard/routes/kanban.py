"""Dashboard routes: kanban.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import kanban


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # A per-bot-instance kanban board — see bot/kanban.py, /kanban.

    @app.get("/api/kanban/boards", dependencies=[Depends(_require_token_or_api_key)])
    def api_kanban_boards(instance_id: int):
        return {"boards": kanban.list_boards(instance_id)}

    @app.get("/api/kanban/cards", dependencies=[Depends(_require_token_or_api_key)])
    def api_kanban_cards(instance_id: int, board: str = "default"):
        return {"cards": kanban.list_cards(instance_id, board)}

    @app.post("/api/kanban/cards", dependencies=[Depends(_require_token_or_api_key)])
    def api_kanban_add_card(payload: dict = Body(...)):
        try:
            card = kanban.add_card(
                int(payload["instance_id"]), payload.get("board", "default"),
                payload.get("column", "todo"), payload.get("text", ""),
            )
        except kanban.KanbanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "card": card}

    @app.post("/api/kanban/cards/{card_id}/move", dependencies=[Depends(_require_token_or_api_key)])
    def api_kanban_move_card(card_id: int, payload: dict = Body(...)):
        try:
            card = kanban.move_card(int(payload["instance_id"]), card_id, payload.get("column", "todo"))
        except kanban.KanbanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "card": card}

    @app.delete("/api/kanban/cards/{card_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_kanban_delete_card(card_id: int, instance_id: int):
        ok = kanban.delete_card(instance_id, card_id)
        if not ok:
            raise HTTPException(status_code=404, detail="card not found")
        return {"ok": True}
