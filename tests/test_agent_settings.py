"""bot/agent_settings.py — the unified agent/swarm control settings
surface: max_concurrent_children, worker provider/model/effort, and
manager effort, resolved through a three-level fallback (this instance's
own row -> the process-wide default row -> hardcoded constants).
"""
from __future__ import annotations

import pytest

from bot import agent_settings, bot_instances
from bot.agent_runtime.subagents import DEFAULT_MAX_CONCURRENT_CHILDREN


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


def test_defaults_when_nothing_configured(temp_db):
    iid = _make_instance()
    resolved = agent_settings.get(iid)
    assert resolved == {
        "max_concurrent_children": DEFAULT_MAX_CONCURRENT_CHILDREN,
        "worker_provider": None,
        "worker_model": None,
        "worker_effort": None,
        "manager_effort": None,
        "fallback_provider": None,
        "fallback_model": None,
        "require_plan_approval": False,
    }


def test_process_wide_default_applies_to_every_instance_without_its_own_row(temp_db):
    iid = _make_instance()
    agent_settings.set_settings(None, max_concurrent_children=3, worker_effort="low")

    resolved = agent_settings.get(iid)
    assert resolved["max_concurrent_children"] == 3
    assert resolved["worker_effort"] == "low"


def test_instance_specific_row_wins_over_process_default(temp_db):
    iid = _make_instance()
    agent_settings.set_settings(None, max_concurrent_children=3)
    agent_settings.set_settings(iid, max_concurrent_children=10)

    assert agent_settings.get(iid)["max_concurrent_children"] == 10
    assert agent_settings.get(None)["max_concurrent_children"] == 3


def test_partial_update_leaves_other_fields_untouched(temp_db):
    iid = _make_instance()
    agent_settings.set_settings(iid, worker_provider="openrouter", worker_model="some/model")
    agent_settings.set_settings(iid, worker_effort="high")

    resolved = agent_settings.get(iid)
    assert resolved["worker_provider"] == "openrouter"
    assert resolved["worker_model"] == "some/model"
    assert resolved["worker_effort"] == "high"


def test_explicit_none_clears_a_field_back_to_fallthrough(temp_db):
    iid = _make_instance()
    agent_settings.set_settings(None, worker_effort="medium")
    agent_settings.set_settings(iid, worker_effort="high")
    assert agent_settings.get(iid)["worker_effort"] == "high"

    agent_settings.set_settings(iid, worker_effort=None)
    assert agent_settings.get(iid)["worker_effort"] == "medium"


def test_unknown_field_is_rejected(temp_db):
    iid = _make_instance()
    with pytest.raises(ValueError, match="unknown"):
        agent_settings.set_settings(iid, not_a_real_field=1)


def test_two_different_instances_never_collide(temp_db):
    a = _make_instance()
    b = bot_instances.create_instance(
        name="worker-2", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    agent_settings.set_settings(a, worker_model="model-a")
    agent_settings.set_settings(b, worker_model="model-b")

    assert agent_settings.get(a)["worker_model"] == "model-a"
    assert agent_settings.get(b)["worker_model"] == "model-b"
