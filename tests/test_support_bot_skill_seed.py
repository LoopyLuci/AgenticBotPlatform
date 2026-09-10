"""bot/dashboard/server.py's _seed_support_bot_training_ops_skill() —
registers the global "support-bot-training-ops" runtime skill exactly
once (next-generation modular hybrid plan, Phase 9's reusable surfaces).
"""
from __future__ import annotations

from bot import db
from bot.dashboard.server import build_app


def test_build_app_seeds_the_support_bot_skill(temp_db):
    build_app()

    row = db.get_skill(None, "support-bot-training-ops")
    assert row is not None
    assert "support_bot_" in row["content"]


def test_seeding_is_idempotent_and_never_overwrites_an_edit(temp_db):
    build_app()
    db.get_conn().execute(
        "UPDATE skills SET content=? WHERE name=? AND instance_id IS NULL",
        ("an operator's own custom edit", "support-bot-training-ops"),
    )
    db.get_conn().commit()

    build_app()  # a second build_app() call must not stomp the edit

    row = db.get_skill(None, "support-bot-training-ops")
    assert row["content"] == "an operator's own custom edit"
