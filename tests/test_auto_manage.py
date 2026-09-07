"""bot/auto_manage.py — autonomous manager check-ins, configurable per
instance as scheduled, reactive (a new kanban card), or both, per the
user's explicit choice. Both triggers funnel through run_check_in(), so
tests exercise it directly plus each trigger's own wiring.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import auto_manage, bot_instances, db, kanban, scheduler
from bot.backends.base import BackendResult


def _run(coro):
    return asyncio.run(coro)


def _make_manager_instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1], persona="manager",
    )


class TestConfig:
    def test_get_config_defaults_to_disabled(self, temp_db):
        iid = _make_manager_instance()
        assert auto_manage.get_config(iid) == {"enabled": False}

    def test_enable_scheduled_creates_a_real_schedule_row(self, temp_db):
        iid = _make_manager_instance()
        cfg = auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="1h")

        assert cfg["enabled"] is True
        assert cfg["schedule_id"]
        rows = scheduler.list_for_chat(iid, chat_id=42)
        assert any(r["id"] == cfg["schedule_id"] and r["kind"] == "auto_manage" for r in rows)

    def test_enable_reactive_only_creates_no_schedule_row(self, temp_db):
        iid = _make_manager_instance()
        cfg = auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created")

        assert cfg["schedule_id"] is None
        assert scheduler.list_for_chat(iid, chat_id=42) == []

    def test_disable_removes_the_schedule_row(self, temp_db):
        iid = _make_manager_instance()
        cfg = auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="1h")
        sched_id = cfg["schedule_id"]

        auto_manage.disable(iid)

        assert auto_manage.get_config(iid)["enabled"] is False
        rows = scheduler.list_for_chat(iid, chat_id=42)
        assert not any(r["id"] == sched_id for r in rows)

    def test_re_enabling_removes_the_old_schedule_row(self, temp_db):
        iid = _make_manager_instance()
        first = auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="1h")
        second = auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="2h")

        rows = scheduler.list_for_chat(iid, chat_id=42)
        ids = {r["id"] for r in rows}
        assert first["schedule_id"] not in ids
        assert second["schedule_id"] in ids

    def test_invalid_trigger_raises(self, temp_db):
        iid = _make_manager_instance()
        with pytest.raises(auto_manage.AutoManageError):
            auto_manage.enable(iid, chat_id=42, trigger="not_a_real_trigger")

    def test_unknown_instance_raises(self, temp_db):
        with pytest.raises(auto_manage.AutoManageError):
            auto_manage.get_config(999999)


class TestRunCheckIn:
    def test_disabled_is_a_no_op(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        called = {"n": 0}

        async def fake_run_turn(*a, **kw):
            called["n"] += 1
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)
        _run(auto_manage.run_check_in(iid, reason="test"))
        assert called["n"] == 0

    def test_no_chat_id_is_a_no_op(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        auto_manage.set_config(iid, enabled=True)  # enabled but no chat_id ever set
        called = {"n": 0}

        async def fake_run_turn(*a, **kw):
            called["n"] += 1
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)
        _run(auto_manage.run_check_in(iid, reason="test"))
        assert called["n"] == 0

    def test_enabled_with_chat_id_calls_run_turn_with_the_goal_template(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created")
        captured = {}

        async def fake_run_turn(goal, **kw):
            captured["goal"] = goal
            captured["kw"] = kw
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)
        _run(auto_manage.run_check_in(iid, reason="a new card"))

        assert "a new card" in captured["goal"]
        assert captured["kw"]["instance_id"] == iid
        assert captured["kw"]["chat_id"] == 42
        assert captured["kw"]["background"] is True

    def test_custom_goal_template_is_used(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created", goal_template="Custom: {reason}")
        captured = {}

        async def fake_run_turn(goal, **kw):
            captured["goal"] = goal
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)
        _run(auto_manage.run_check_in(iid, reason="xyz"))

        assert captured["goal"] == "Custom: xyz"


class TestKanbanReactiveTrigger:
    def test_card_creation_triggers_a_check_in_when_configured(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created")
        captured = {}

        async def fake_run_turn(goal, **kw):
            captured["goal"] = goal
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)

        card = kanban.add_card(iid, "default", "todo", "a new task")
        _run(auto_manage.maybe_trigger_from_kanban_card(card["id"]))

        assert f"kanban card #{card['id']}" in captured["goal"]

    def test_card_creation_is_ignored_when_trigger_is_scheduled_only(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="1h")
        called = {"n": 0}

        async def fake_run_turn(*a, **kw):
            called["n"] += 1
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)

        card = kanban.add_card(iid, "default", "todo", "a new task")
        _run(auto_manage.maybe_trigger_from_kanban_card(card["id"]))

        assert called["n"] == 0

    def test_card_in_a_different_instances_board_never_triggers(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        other_id = bot_instances.create_instance(
            name="other", platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
            allowed_user_ids=[1],
        )
        auto_manage.enable(iid, chat_id=42, trigger="kanban_card_created")
        called = {"n": 0}

        async def fake_run_turn(*a, **kw):
            called["n"] += 1
            return ("ran", None)

        monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)

        card = kanban.add_card(other_id, "default", "todo", "a new task")
        _run(auto_manage.maybe_trigger_from_kanban_card(card["id"]))

        assert called["n"] == 0


class TestSchedulerIntegration:
    def test_auto_manage_kind_row_dispatches_through_run_check_in(self, temp_db, monkeypatch):
        iid = _make_manager_instance()
        cfg = auto_manage.enable(iid, chat_id=42, trigger="scheduled", interval="5s")
        captured = {}

        async def fake_run_check_in(instance_id, reason):
            captured["instance_id"] = instance_id
            captured["reason"] = reason

        monkeypatch.setattr(auto_manage, "run_check_in", fake_run_check_in)

        target = next(r for r in scheduler.list_for_chat(iid, chat_id=42) if r["id"] == cfg["schedule_id"])
        _run(scheduler._fire(target))

        assert captured["instance_id"] == iid
        assert captured["reason"] == "scheduled check-in"
