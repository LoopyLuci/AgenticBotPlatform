"""bot/agent_runtime/estop.py — the global emergency stop, mirroring
Hermes Agent's own estop.py concept (a global pause checked by
long-running components before starting new work). Checked at the top of
every new-turn/new-dispatch entry point, never mid-turn.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import estop
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


def test_disengaged_by_default(temp_db):
    assert not estop.is_engaged()
    assert estop.status()["engaged"] is False


def test_engage_sets_reason_and_actor(temp_db):
    result = estop.engage("testing a runaway swarm", actor="operator")

    assert result["engaged"] is True
    assert result["reason"] == "testing a runaway swarm"
    assert result["actor"] == "operator"
    assert estop.is_engaged()


def test_disengage_clears_it(temp_db):
    estop.engage("stop", actor="operator")
    result = estop.disengage(actor="operator")

    assert result["engaged"] is False
    assert result["reason"] is None
    assert not estop.is_engaged()


def test_check_raises_when_engaged(temp_db):
    estop.engage("bad swarm", actor="operator")

    with pytest.raises(estop.EstopEngagedError, match="bad swarm"):
        estop.check()


def test_check_is_a_backend_error_subclass(temp_db):
    """So every existing `except BackendError` handler throughout the
    codebase catches this without a separate case."""
    estop.engage(actor="operator")
    with pytest.raises(BackendError):
        estop.check()


def test_check_is_a_noop_when_disengaged(temp_db):
    estop.check()  # must not raise


class TestWiredEntryPoints:
    def test_native_backend_ask_refuses_when_engaged(self, temp_db):
        from bot.backends.native_backend import NativeAgentBackend

        estop.engage("halt", actor="operator")
        backend = NativeAgentBackend(transport=None, model="fake-model", session_prefix="test", name="native_agent")

        with pytest.raises(estop.EstopEngagedError):
            _run(backend.ask("hi", context={"instance_id": 1}))

    def test_run_batch_refuses_when_engaged(self, temp_db):
        from bot.agent_runtime import subagents

        estop.engage("halt", actor="operator")

        with pytest.raises(estop.EstopEngagedError):
            _run(subagents.run_batch([{"goal": "x"}], parent_instance_id=1))

    def test_scheduler_fire_silently_skips_when_engaged(self, temp_db):
        from bot import bot_instances
        from bot import scheduler

        iid = bot_instances.create_instance(
            name="worker", platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
            allowed_user_ids=[1],
        )
        estop.engage("halt", actor="operator")
        row = {"id": 1, "instance_id": iid, "chat_id": 1, "thread_id": None, "kind": "prompt", "prompt": "x", "interval_s": 60}

        _run(scheduler._fire(row))  # must not raise, must not dispatch

    def test_auto_manage_run_check_in_skips_when_engaged(self, temp_db):
        from bot import auto_manage, bot_instances

        iid = bot_instances.create_instance(
            name="manager", platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
            allowed_user_ids=[1], persona="manager",
        )
        auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created")
        estop.engage("halt", actor="operator")

        called = {"n": 0}

        async def fake_run_turn(*a, **kw):
            called["n"] += 1
            return ("ran", None)

        import bot.agent_runtime.engine as agent_engine
        original = agent_engine.run_turn
        agent_engine.run_turn = fake_run_turn
        try:
            _run(auto_manage.run_check_in(iid, reason="test"))
        finally:
            agent_engine.run_turn = original

        assert called["n"] == 0
