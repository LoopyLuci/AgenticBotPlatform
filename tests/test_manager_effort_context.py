"""Router.ask() must inject the manager/orchestrator's own effort
(bot/agent_settings.py's manager_effort field) into context["effort"] for
its own turn — distinct from a spawned child's "worker_effort", which
subagents.py resolves separately for direct backend.ask() calls that
never pass through Router.ask() at all.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import agent_settings, bot_instances
from bot.backends.base import BackendResult
from bot.config import config
from bot.router import Router


def _make_instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


@pytest.fixture
def router_with_recording_backend(monkeypatch, temp_db):
    monkeypatch.setattr(config, "_data", {"default_backend": "api", "action_overrides": {}, "timeouts": {}})
    monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))

    r = Router()
    calls = []

    class _RecordingBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            calls.append(context or {})
            return BackendResult(text="ok", tokens=None, raw=None)

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None, hermes_home=None: _RecordingBackend())
    return r, calls


def _ask(router, **kw):
    return asyncio.run(router.ask("hi", **kw))


def test_manager_effort_reaches_context(temp_db, router_with_recording_backend):
    router, calls = router_with_recording_backend
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, manager_effort="xhigh")

    _ask(router, instance_id=instance_id)

    assert calls[0]["effort"] == "xhigh"


def test_no_manager_effort_configured_is_none(temp_db, router_with_recording_backend):
    router, calls = router_with_recording_backend
    instance_id = _make_instance()

    _ask(router, instance_id=instance_id)

    assert calls[0]["effort"] is None


def test_process_wide_default_manager_effort_applies(temp_db, router_with_recording_backend):
    router, calls = router_with_recording_backend
    instance_id = _make_instance()
    agent_settings.set_settings(None, manager_effort="low")

    _ask(router, instance_id=instance_id)

    assert calls[0]["effort"] == "low"
