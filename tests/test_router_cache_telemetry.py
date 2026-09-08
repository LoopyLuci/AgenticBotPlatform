"""Router.ask() logs prompt-caching telemetry (cache_creation_tokens/
cache_read_tokens) from BackendResult.raw when present — Phase A of the
Claude API/Claude Code parity plan. Mirrors test_manager_effort_context.py's
own _RecordingBackend pattern.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, db
from bot.backends.base import BackendResult
from bot.config import config
from bot.router import Router


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


@pytest.fixture
def router_with_fake_backend(monkeypatch, temp_db):
    monkeypatch.setattr(config, "_data", {"default_backend": "api", "action_overrides": {}, "timeouts": {}})
    monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))

    r = Router()
    result_holder = {}

    class _FakeBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            return result_holder["result"]

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None, hermes_home=None: _FakeBackend())
    return r, result_holder


def _ask(router, **kw):
    return asyncio.run(router.ask("hi", **kw))


def test_cache_telemetry_logged_when_present(temp_db, router_with_fake_backend, monkeypatch):
    router, result_holder = router_with_fake_backend
    result_holder["result"] = BackendResult(text="ok", tokens=8, raw={"cache_creation_tokens": 1370, "cache_read_tokens": 5})
    logged = []
    monkeypatch.setattr(db, "log_telemetry", lambda component, metric, value: logged.append((component, metric, value)))

    _ask(router, instance_id=_make_instance())

    assert ("api", "cache_creation_tokens", 1370) in logged
    assert ("api", "cache_read_tokens", 5) in logged


def test_no_cache_telemetry_logged_when_absent(temp_db, router_with_fake_backend, monkeypatch):
    router, result_holder = router_with_fake_backend
    result_holder["result"] = BackendResult(text="ok", tokens=8, raw={"total_tokens": 8})
    logged = []
    monkeypatch.setattr(db, "log_telemetry", lambda component, metric, value: logged.append((component, metric, value)))

    _ask(router, instance_id=_make_instance())

    assert not any(m in ("cache_creation_tokens", "cache_read_tokens") for _, m, _ in logged)
