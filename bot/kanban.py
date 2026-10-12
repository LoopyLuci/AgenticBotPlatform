"""A per-instance kanban board — /kanban. Real persistence (bot/db.py's
kanban_boards/kanban_cards tables), scoped down from the real Hermes
Agent's 30+ subcommand multi-profile board to the core operations that
don't depend on anything AgenticBotPlatform doesn't have (no multi-profile
collaboration layer here — one board set per bot instance).
"""

from __future__ import annotations


from bot import db

DEFAULT_COLUMNS = ("todo", "doing", "done")


class KanbanError(Exception):
    pass


def get_or_create_board(instance_id: int, name: str) -> int:
    return db.get_or_create_kanban_board(instance_id, name.strip() or "default")


def list_boards(instance_id: int) -> list[dict]:
    return [dict(r) for r in db.list_kanban_boards(instance_id)]


def add_card(instance_id: int, board_name: str, column: str, text: str) -> dict:
    if not text.strip():
        raise KanbanError("card text can't be empty")
    board_id = get_or_create_board(instance_id, board_name)
    card_id = db.create_kanban_card(board_id, column.strip() or "todo", text.strip())
    return dict(db.get_kanban_card(card_id))


def list_cards(instance_id: int, board_name: str) -> list[dict]:
    board = db.get_or_create_kanban_board(instance_id, board_name.strip() or "default")
    return [dict(r) for r in db.list_kanban_cards(board)]


def move_card(instance_id: int, card_id: int, column: str) -> dict:
    card = db.get_kanban_card(card_id)
    if card is None:
        raise KanbanError(f"card #{card_id} not found")
    board = db.get_kanban_board(card["board_id"])
    if board is None or board["instance_id"] != instance_id:
        raise KanbanError(f"card #{card_id} not found")
    db.move_kanban_card(card_id, column.strip() or "todo")
    return dict(db.get_kanban_card(card_id))


def delete_card(instance_id: int, card_id: int) -> bool:
    card = db.get_kanban_card(card_id)
    if card is None:
        return False
    board = db.get_kanban_board(card["board_id"])
    if board is None or board["instance_id"] != instance_id:
        return False
    db.delete_kanban_card(card_id)
    return True


def format_board(instance_id: int, board_name: str) -> str:
    cards = list_cards(instance_id, board_name)
    if not cards:
        return f"Board {board_name!r} is empty. Use /kanban add {board_name} <column> <text>."
    by_col: dict[str, list[dict]] = {}
    for c in cards:
        by_col.setdefault(c["column_name"], []).append(c)
    lines = [f"Board: {board_name}"]
    for col in DEFAULT_COLUMNS + tuple(c for c in by_col if c not in DEFAULT_COLUMNS):
        if col not in by_col:
            continue
        lines.append(f"\n{col}:")
        for c in by_col[col]:
            lines.append(f"  #{c['id']} {c['text']}")
    return "\n".join(lines)


# ------------------------------------------------------------- work board mirror
# A read-only view of the work board (bot/coordination) as kanban boards: one board per board name, one column per task
# state, one card per task. These are NOT the hand-made cards above - they live on the work board, carry `source:
# "work-board"` and `read_only: true`, and nothing here (or in the routes, CLI or screens on top) writes to the board.
WORK_BOARD_COLUMNS = ("open", "claimed", "running", "review", "done", "failed", "cancelled")


def work_board_mirror(board: str | None = None, *, limit: int = 500) -> dict:
    from bot import coordination
    from bot.coordination.model import BoardError

    try:
        used = coordination.get_board()
        tasks = used.list_tasks(board or None, limit=max(1, min(int(limit), 1000)))
    except BoardError as exc:
        raise KanbanError(f"the work board is not available: {exc.message}") from exc
    boards: dict[str, dict[str, list[dict]]] = {}
    for t in tasks:
        cols = boards.setdefault(t["board"], {c: [] for c in WORK_BOARD_COLUMNS})
        card = {"id": t["id"], "title": t["title"], "state": t["state"], "owner": t.get("owner"),
                "priority": t.get("priority", 0), "attempts": t.get("attempts", 0),
                "max_attempts": t.get("max_attempts"), "lease_until": t.get("lease_until"),
                "note": t.get("note") or "", "deps": list(t.get("deps") or []),
                "source": "work-board", "read_only": True}
        cols.setdefault(t["state"], []).append(card)
    return {"source": "work-board", "read_only": True, "backend": used.name,
            "boards": [{"board": name, "columns": [{"name": c, "cards": cols[c]} for c in cols],
                        "total": sum(len(v) for v in cols.values())} for name, cols in sorted(boards.items())]}


def format_work_board(board: str | None = None) -> str:
    mirror = work_board_mirror(board)
    if not mirror["boards"]:
        return "The work board has no tasks" + (f" on {board!r}." if board else ".")
    lines = ["Work board (read-only mirror - not your kanban cards)"]
    for b in mirror["boards"]:
        lines.append(f"\nBoard: {b['board']}")
        for col in b["columns"]:
            if not col["cards"]:
                continue
            lines.append(f"  {col['name']}:")
            for c in col["cards"]:
                who = f" [{c['owner']}]" if c["owner"] else ""
                lines.append(f"    {c['id'][:8]} {c['title']}{who}")
    return "\n".join(lines)
