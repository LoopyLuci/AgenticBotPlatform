"""/estop (bot.commands.cmd_estop)."""
from __future__ import annotations

import asyncio

from bot import commands
from bot.agent_runtime import estop
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return CmdContext(instance_id=1, instance_name="test", user_id=1, chat_id=1, actor="test")


def test_status_default(temp_db):
    reply = _run(commands.cmd_estop(_ctx(), []))
    assert "not engaged" in reply.lower()


def test_status_is_the_default_with_no_args(temp_db):
    reply = _run(commands.cmd_estop(_ctx(), []))
    assert "not engaged" in reply.lower()


def test_engage_with_reason(temp_db):
    reply = _run(commands.cmd_estop(_ctx(), ["engage", "runaway", "swarm"]))
    assert "ENGAGED" in reply
    assert estop.status()["reason"] == "runaway swarm"


def test_status_after_engage_shows_reason_and_actor(temp_db):
    _run(commands.cmd_estop(_ctx(), ["engage", "bad", "loop"]))
    reply = _run(commands.cmd_estop(_ctx(), ["status"]))
    assert "ENGAGED" in reply
    assert "bad loop" in reply
    assert "test" in reply


def test_disengage(temp_db):
    _run(commands.cmd_estop(_ctx(), ["engage"]))
    reply = _run(commands.cmd_estop(_ctx(), ["disengage"]))
    assert "disengaged" in reply.lower()
    assert not estop.is_engaged()
