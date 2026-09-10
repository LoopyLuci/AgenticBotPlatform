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
# Same rationale, for bot/support_bot/module_manifest.py's per-Knowledge-
# Module persisted models and manifest — see that module's own comment.
os.environ.setdefault(
    "BOTSERVER_SUPPORT_BOT_MODULES_DIR",
    str(Path(tempfile.gettempdir()) / "botserver_pytest_support_bot_modules"),
)
os.environ.setdefault(
    "BOTSERVER_SUPPORT_BOT_MANIFEST_PATH",
    str(Path(tempfile.gettempdir()) / "botserver_pytest_support_bot_manifest.json"),
)

import pytest

from bot import db as db_module


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_module, "_conn", None)
    conn = db_module.get_conn()
    db_module.init_db()
    # bot_instances.BACKUP_DIR is a module-level constant pointing at the
    # REAL data/bot_instances_backups/ — not test-isolated by the DB-path
    # patch above. Without this, every test that creates/updates/deletes a
    # bot instance (a lot of them) leaks a real JSON file into the shared
    # project directory forever. Confirmed: this was unpatched for the
    # project's entire history and left 47,000+ files there, which in turn
    # made the dashboard's Bots-tab backups table (no row cap) render an
    # enormous DOM and made the whole app sluggish to resize/scroll.
    from bot import bot_instances as bot_instances_module

    monkeypatch.setattr(bot_instances_module, "BACKUP_DIR", tmp_path / "bot_instances_backups")
    yield conn
    conn.close()
    monkeypatch.setattr(db_module, "_conn", None)
