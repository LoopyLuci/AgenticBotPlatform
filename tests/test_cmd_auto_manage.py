"""/auto_manage (bot.commands.cmd_auto_manage) — the Telegram-facing
control for bot/auto_manage.py.
"""
from __future__ import annotations

import asyncio

from bot import auto_manage, bot_instances, commands
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(instance_id, chat_id=1):
    return CmdContext(instance_id=instance_id, instance_name="test", user_id=1, chat_id=chat_id, actor="test")


def _make_manager_instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1], persona="manager",
    )


def _make_non_manager_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1],
    )


def test_show_when_off(temp_db):
    iid = _make_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid), []))
    assert "off" in reply.lower()


def test_enable_requires_manager_persona(temp_db):
    iid = _make_non_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["enable"]))
    assert "manager" in reply.lower()
    assert not auto_manage.get_config(iid).get("enabled")


def test_enable_allowed_for_auto_orchestrator_persona(temp_db):
    iid = bot_instances.create_instance(
        name="auto-orch", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1], persona="auto_orchestrator",
    )
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["enable"]))
    assert "enabled" in reply.lower()


def test_enable_then_show(temp_db):
    iid = _make_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid, chat_id=99), ["enable"]))
    assert "enabled" in reply.lower()

    shown = _run(commands.cmd_auto_manage(_ctx(iid), ["show"]))
    assert "enabled: True" in shown
    assert "chat_id: 99" in shown


def test_disable(temp_db):
    iid = _make_manager_instance()
    _run(commands.cmd_auto_manage(_ctx(iid), ["enable"]))
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["disable"]))
    assert "disabled" in reply.lower()
    assert not auto_manage.get_config(iid).get("enabled")


def test_set_trigger_before_enabling(temp_db):
    iid = _make_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["set-trigger", "both"]))
    assert "both" in reply
    assert auto_manage.get_config(iid).get("trigger") == "both"


def test_set_trigger_rejects_unknown_value(temp_db):
    iid = _make_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["set-trigger", "nonsense"]))
    assert "must be one of" in reply


def test_set_interval_while_enabled_recreates_the_schedule(temp_db):
    iid = _make_manager_instance()
    _run(commands.cmd_auto_manage(_ctx(iid), ["enable"]))
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["set-interval", "2h"]))
    assert "2h" in reply
    assert auto_manage.get_config(iid)["interval"] == "2h"


def test_set_goal(temp_db):
    iid = _make_manager_instance()
    reply = _run(commands.cmd_auto_manage(_ctx(iid), ["set-goal", "Check", "the", "kanban", "board"]))
    assert "updated" in reply.lower()
    assert auto_manage.get_config(iid)["goal_template"] == "Check the kanban board"


def test_no_instance_is_a_clear_error(temp_db):
    reply = _run(commands.cmd_auto_manage(_ctx(None), []))
    assert "no bot instance" in reply.lower()
