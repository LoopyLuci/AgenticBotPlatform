"""Shared pytest fixtures.

`temp_db` points bot.db at a fresh, throwaway SQLite file for the
duration of one test — never the real data/bot.db. This is the same
monkeypatch-DB_PATH-and-reset-the-cached-connection pattern used by every
ad-hoc verification script earlier in this project's history, just made
reusable and permanent instead of hand-written and discarded each time.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Must run BEFORE any `from bot...` import anywhere in the test session —
# bot/support_bot/hybrid.py loads its persisted classifier state from
# bot.support_bot.model_io.CURRENT_PATH once, at module import time, not
# per-call. Without this override, a real data/support_bot_models/current.json
# left behind by live desktop testing in this same checkout would
# silently win over what every Support Bot test expects to be trained
# fresh from the current bot/support_bot/training_data.py. See
# model_io.py's own comment on CURRENT_PATH for the full explanation.
os.environ.setdefault(
    "BOTSERVER_SUPPORT_BOT_MODEL_PATH",
    str(Path(tempfile.gettempdir()) / "botserver_pytest_support_bot_model.json"),
)

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
