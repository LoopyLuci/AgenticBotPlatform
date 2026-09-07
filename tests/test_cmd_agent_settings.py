"""/agent_settings (bot.commands.cmd_agent_settings) — the Telegram-facing
control for bot/agent_settings.py's unified concurrency/worker-model/
effort settings, plus the Hermes-backed special case (worker_effort/
manager_effort write through to Hermes's own real config instead).
"""
from __future__ import annotations

import asyncio

from bot import agent_settings, bot_instances, commands, hermes_config
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(instance_id):
    return CmdContext(instance_id=instance_id, instance_name="test", user_id=1, chat_id=1, actor="test")


def _make_api_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1],
    )


def _make_hermes_instance(tmp_path):
    return bot_instances.create_instance(
        name="hermes-worker", platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1], hermes_home=str(tmp_path / "hermes-home"),
    )


def test_show_with_no_args(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), []))
    assert "max_concurrent_children" in reply
    assert "worker_effort" in reply
    assert "manager_effort" in reply


def test_set_max_concurrent_children(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "max_concurrent_children", "3"]))
    assert "3" in reply
    assert agent_settings.get(iid)["max_concurrent_children"] == 3


def test_set_max_concurrent_children_rejects_non_integer(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "max_concurrent_children", "abc"]))
    assert "positive integer" in reply


def test_set_worker_provider_and_model(temp_db):
    iid = _make_api_instance()
    _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_provider", "openrouter"]))
    _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_model", "some/model"]))
    resolved = agent_settings.get(iid)
    assert resolved["worker_provider"] == "openrouter"
    assert resolved["worker_model"] == "some/model"


def test_set_worker_effort_for_api_instance(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_effort", "low"]))
    assert "low" in reply
    assert agent_settings.get(iid)["worker_effort"] == "low"


def test_set_worker_effort_rejects_unknown_level(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_effort", "extreme"]))
    assert "unknown effort level" in reply


def test_clear_a_field(temp_db):
    iid = _make_api_instance()
    _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_effort", "high"]))
    assert agent_settings.get(iid)["worker_effort"] == "high"

    _run(commands.cmd_agent_settings(_ctx(iid), ["clear", "worker_effort"]))
    assert agent_settings.get(iid)["worker_effort"] is None


def test_unknown_field_is_a_clear_error(temp_db):
    iid = _make_api_instance()
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "not_a_field", "x"]))
    assert "unknown field" in reply


def test_hermes_backed_worker_effort_writes_through_to_delegation_config(temp_db, tmp_path):
    iid = _make_hermes_instance(tmp_path)
    reply = _run(commands.cmd_agent_settings(_ctx(iid), ["set", "worker_effort", "xhigh"]))
    assert "Hermes config" in reply

    assert hermes_config.read_delegation_config(hermes_home=str(tmp_path / "hermes-home"))["reasoning_effort"] == "xhigh"
    # Must NOT land in the generic agent_settings table for a Hermes instance.
    assert agent_settings.get(iid)["worker_effort"] is None


def test_hermes_backed_manager_effort_writes_through_to_agent_config(temp_db, tmp_path):
    iid = _make_hermes_instance(tmp_path)
    _run(commands.cmd_agent_settings(_ctx(iid), ["set", "manager_effort", "minimal"]))

    assert hermes_config.read_agent_config(hermes_home=str(tmp_path / "hermes-home"))["reasoning_effort"] == "minimal"


def test_no_instance_is_a_clear_error(temp_db):
    reply = _run(commands.cmd_agent_settings(_ctx(None), []))
    assert "no bot instance" in reply.lower()
