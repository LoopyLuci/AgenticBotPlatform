"""Kanban boards and cards.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Optional

from bot import db as _db


def get_or_create_kanban_board(instance_id: int, name: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT id FROM kanban_boards WHERE instance_id=? AND name=?", (instance_id, name)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO kanban_boards (instance_id, name, created_at) VALUES (?, ?, ?)",
            (instance_id, name, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_kanban_boards(instance_id: int) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM kanban_boards WHERE instance_id=? ORDER BY name", (instance_id,)).fetchall()


def get_kanban_board(board_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM kanban_boards WHERE id=?", (board_id,)).fetchone()


def create_kanban_card(board_id: int, column_name: str, text: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        pos_row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM kanban_cards WHERE board_id=? AND column_name=?",
            (board_id, column_name),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO kanban_cards (board_id, column_name, text, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (board_id, column_name, text, pos_row["p"], _db._now(), _db._now()),
        )
        conn.commit()
        card_id = cur.lastrowid
    _db._notify_kanban_card_created(card_id)
    return card_id


def list_kanban_cards(board_id: int) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM kanban_cards WHERE board_id=? ORDER BY column_name, position", (board_id,)
    ).fetchall()


def get_kanban_card(card_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM kanban_cards WHERE id=?", (card_id,)).fetchone()


def move_kanban_card(card_id: int, column_name: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE kanban_cards SET column_name=?, updated_at=? WHERE id=?", (column_name, _db._now(), card_id)
        )
        conn.commit()


def delete_kanban_card(card_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM kanban_cards WHERE id=?", (card_id,))
        conn.commit()
