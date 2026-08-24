"""A per-instance kanban board — /kanban. Real persistence (bot/db.py's
kanban_boards/kanban_cards tables), scoped down from the real Hermes
Agent's 30+ subcommand multi-profile board to the core operations that
don't depend on anything BotServer doesn't have (no multi-profile
collaboration layer here — one board set per bot instance).
"""

from __future__ import annotations

from typing import Optional

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
