"""/effort (bot.commands.cmd_effort) — the Telegram-facing control for
Claude Desktop's own "Effort" toolbar slider (ui backend only). Confirmed
live: that slider defaults to High and is forced back down before every
automated send (see bot/backends/ui_backend.py's EFFORT_LEVELS); this
command is how a user adjusts the stored target level without editing the
database by hand.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, commands
from bot.backends.ui_backend import EFFORT_LEVELS
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(instance_id):
    return CmdContext(instance_id=instance_id, instance_name="test", user_id=1, chat_id=1, actor="test")


def _make_ui_instance(temp_db):
    return bot_instances.create_instance(
        name="ui-bot", platform="telegram", backend="ui",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFatherPadding123"},
        allowed_user_ids=[1],
    )


def test_shows_the_low_default_with_no_args(temp_db):
    iid = _make_ui_instance(temp_db)
    reply = _run(commands.cmd_effort(_ctx(iid), []))
    assert "low" in reply.lower()


def test_sets_a_new_level_and_persists_it(temp_db):
    iid = _make_ui_instance(temp_db)
    reply = _run(commands.cmd_effort(_ctx(iid), ["high"]))
    assert "high" in reply.lower()
    assert bot_instances.get_instance(iid)["desktop_effort"] == "high"


def test_rejects_an_unknown_level(temp_db):
    iid = _make_ui_instance(temp_db)
    reply = _run(commands.cmd_effort(_ctx(iid), ["extreme"]))
    assert "unknown" in reply.lower()
    assert bot_instances.get_instance(iid)["desktop_effort"] == "low"


def test_no_instance_is_a_clear_error(temp_db):
    reply = _run(commands.cmd_effort(_ctx(None), []))
    assert "no bot instance" in reply.lower()


def test_every_level_name_round_trips(temp_db):
    iid = _make_ui_instance(temp_db)
    for level in EFFORT_LEVELS:
        _run(commands.cmd_effort(_ctx(iid), [level]))
        assert bot_instances.get_instance(iid)["desktop_effort"] == level
