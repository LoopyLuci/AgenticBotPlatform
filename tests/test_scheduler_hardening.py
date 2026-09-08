"""bot/scheduler.py's Phase G hardening (native-BotServer-agents plan):
preflight validation at create() time, and failure-streak tracking that
auto-disables a row and sends exactly one alert once it crosses
scheduler.max_consecutive_failures.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, db, scheduler
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _make_instance(**overrides):
    kwargs = dict(
        name="sched-bot", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


# --------------------------------------------------------- create() -----

def test_create_rejects_a_nonexistent_instance(temp_db):
    with pytest.raises(scheduler.ScheduleError, match="no bot instance"):
        scheduler.create(999999, "42", "cron", "say hi", 60)


def test_create_accepts_a_disabled_instance(temp_db):
    # enabled=False is a normal staged/paused state, not proof the
    # schedule is invalid — see scheduler.create()'s own comment.
    instance_id = _make_instance(enabled=False)
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    assert sched_id is not None


def test_create_accepts_an_enabled_instance(temp_db):
    instance_id = _make_instance(enabled=True)
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    assert sched_id is not None


# --------------------------------------------------- failure streak -----

def _row(sched_id):
    return db.get_scheduled_command(sched_id)


def test_a_success_resets_the_streak(temp_db):
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    db.record_scheduled_command_failure(sched_id, "boom")
    db.record_scheduled_command_failure(sched_id, "boom again")
    assert _row(sched_id)["consecutive_failures"] == 2

    db.reset_scheduled_command_failures(sched_id)

    assert _row(sched_id)["consecutive_failures"] == 0


def test_record_failure_stores_the_error_and_increments(temp_db):
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)

    count = db.record_scheduled_command_failure(sched_id, "connection refused")

    assert count == 1
    row = _row(sched_id)
    assert row["last_error"] == "connection refused"
    assert row["consecutive_failures"] == 1


@pytest.fixture
def _low_threshold(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "scheduler": {"max_consecutive_failures": 3}})
    yield
    monkeypatch.setattr(config, "_data", {k: v for k, v in config._data.items() if k != "scheduler"})


def test_crossing_the_threshold_auto_disables_and_alerts_exactly_once(temp_db, monkeypatch, _low_threshold):
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    row = _row(sched_id)

    sent = []

    async def _fake_send(instance_id, chat_id, text, thread_id=None):
        sent.append(text)

    monkeypatch.setattr("bot.outbox.send_message", _fake_send)

    _run(scheduler._record_failure(row, "error 1"))
    assert bool(_row(sched_id)["enabled"]) is True
    assert sent == []

    _run(scheduler._record_failure(row, "error 2"))
    assert bool(_row(sched_id)["enabled"]) is True
    assert sent == []

    _run(scheduler._record_failure(row, "error 3 — the one that crosses 3"))
    assert bool(_row(sched_id)["enabled"]) is False
    assert len(sent) == 1
    assert "auto-disabled" in sent[0]
    assert "error 3" in sent[0]


def test_zero_threshold_never_auto_disables(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "scheduler": {"max_consecutive_failures": 0}})
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    row = _row(sched_id)

    sent = []
    monkeypatch.setattr("bot.outbox.send_message", lambda *a, **k: sent.append(a))

    for i in range(10):
        _run(scheduler._record_failure(row, f"error {i}"))

    assert bool(_row(sched_id)["enabled"]) is True
    assert sent == []


def test_deliver_error_outcome_records_a_failure(temp_db, monkeypatch, _low_threshold):
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    row = _row(sched_id)

    called = {}

    async def _fake_run_turn(prompt, *, on_result, **kwargs):
        called["on_result"] = on_result

    monkeypatch.setattr("bot.agent_runtime.engine.run_turn", _fake_run_turn)
    monkeypatch.setattr("bot.agent_runtime.estop.is_engaged", lambda: False)

    _run(scheduler._fire(row))
    _run(called["on_result"]("error", "the backend blew up"))

    assert _row(sched_id)["consecutive_failures"] == 1
    assert _row(sched_id)["last_error"] == "the backend blew up"


def test_deliver_ran_outcome_resets_the_streak(temp_db, monkeypatch):
    instance_id = _make_instance()
    sched_id = scheduler.create(instance_id, "42", "cron", "say hi", 60)
    db.record_scheduled_command_failure(sched_id, "earlier failure")
    row = _row(sched_id)
    assert row["consecutive_failures"] == 1

    called = {}

    async def _fake_run_turn(prompt, *, on_result, **kwargs):
        called["on_result"] = on_result

    async def _fake_send(*a, **k):
        pass

    monkeypatch.setattr("bot.agent_runtime.engine.run_turn", _fake_run_turn)
    monkeypatch.setattr("bot.agent_runtime.estop.is_engaged", lambda: False)
    monkeypatch.setattr("bot.outbox.send_message", _fake_send)

    _run(scheduler._fire(row))

    class _FakeResult:
        text = "all good"

    _run(called["on_result"]("ran", _FakeResult()))

    assert _row(sched_id)["consecutive_failures"] == 0
