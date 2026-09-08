"""/hooks (bot.commands.cmd_hooks) — mirrors test_cmd_mcp_external.py's
own shape for the equivalent /mcp_external command.
"""
from __future__ import annotations

import asyncio

from bot import commands, db
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return CmdContext(instance_id=1, instance_name="test", user_id=1, chat_id=1, actor="test")


def test_list_when_empty(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), []))
    assert "No hooks configured" in reply


def test_add_and_list(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["add", "PreToolUse", "run_shell", "echo", "hi"]))
    assert "Added hook" in reply

    row = db.list_agent_hooks()[0]
    assert row["event"] == "PreToolUse"
    assert row["matcher"] == "run_shell"
    assert row["command"] == "echo hi"

    listed = _run(commands.cmd_hooks(_ctx(), ["list"]))
    assert "PreToolUse" in listed
    assert "run_shell" in listed


def test_add_with_wildcard_matcher(temp_db):
    _run(commands.cmd_hooks(_ctx(), ["add", "SessionStart", "*", "echo", "start"]))
    row = db.list_agent_hooks()[0]
    assert row["matcher"] is None


def test_add_rejects_an_unknown_event(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["add", "NotAnEvent", "*", "echo", "hi"]))
    assert "event must be one of" in reply


def test_add_requires_a_command(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["add", "PreToolUse", "run_shell"]))
    assert "Usage:" in reply


def test_enable_disable_remove_round_trip(temp_db):
    _run(commands.cmd_hooks(_ctx(), ["add", "PreToolUse", "run_shell", "echo", "hi"]))
    hook_id = db.list_agent_hooks()[0]["id"]

    reply = _run(commands.cmd_hooks(_ctx(), ["disable", str(hook_id)]))
    assert "Disabled" in reply
    assert bool(db.get_agent_hook(hook_id)["enabled"]) is False

    reply = _run(commands.cmd_hooks(_ctx(), ["enable", str(hook_id)]))
    assert "Enabled" in reply
    assert bool(db.get_agent_hook(hook_id)["enabled"]) is True

    reply = _run(commands.cmd_hooks(_ctx(), ["remove", str(hook_id)]))
    assert "Removed" in reply
    assert db.get_agent_hook(hook_id) is None


def test_enable_unknown_id(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["enable", "999"]))
    assert "No hook #999" in reply


def test_enable_non_numeric_id(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["enable", "not-a-number"]))
    assert "must be a number" in reply


def test_usage_on_bad_args(temp_db):
    reply = _run(commands.cmd_hooks(_ctx(), ["nonsense"]))
    assert "Usage:" in reply
