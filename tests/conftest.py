"""Shared pytest fixtures.

`temp_db` points bot.db at a fresh, throwaway SQLite file for the
duration of one test — never the real data/bot.db. This is the same
monkeypatch-DB_PATH-and-reset-the-cached-connection pattern used by every
ad-hoc verification script earlier in this project's history, just made
reusable and permanent instead of hand-written and discarded each time.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bot import db as db_module


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_module, "_conn", None)
    conn = db_module.get_conn()
    db_module.init_db()
    yield conn
    conn.close()
    monkeypatch.setattr(db_module, "_conn", None)
