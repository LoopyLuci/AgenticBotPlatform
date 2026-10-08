"""The two feature switches on the Settings page do what they say: "UI automation backend" off means nothing is
routed to the desktop window, and "Verbose telemetry" on logs every backend call, not just the failures."""
from __future__ import annotations

import asyncio
import logging

import pytest

from bot.backends.base import BackendError, BackendResult
from bot.config import config
from bot.router import Router


@pytest.fixture
def router(monkeypatch, temp_db):
    def make(features: dict, chain_backend: str = "api"):
        monkeypatch.setattr(config, "_data", {"default_backend": chain_backend, "action_overrides": {}, "timeouts": {},
                                              "features": features})
        monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))
        r = Router()
        calls = []

        class _FakeBackend:
            model = "m1"

            def __init__(self, name):
                self.name = name

            async def ask(self, prompt, *, context=None, timeout_s=30):
                calls.append(self.name)
                return BackendResult(text="ok", tokens=7, raw={})

        monkeypatch.setattr(r, "_get_backend",
                            lambda name, cfg, model_override=None, hermes_home=None: _FakeBackend(name))
        monkeypatch.setattr(r, "resolve_chain", lambda *a, **k: [chain_backend])
        return r, calls

    return make


def test_ui_backend_refused_when_ui_automation_is_off(router):
    r, calls = router({"ui_automation_enabled": False}, chain_backend="ui")
    with pytest.raises(BackendError, match="UI automation backend is turned off"):
        asyncio.run(r.ask("hi"))
    assert calls == []


def test_ui_backend_used_when_ui_automation_is_on_or_unset(router):
    for features in ({"ui_automation_enabled": True}, {}):
        r, calls = router(features, chain_backend="ui")
        assert asyncio.run(r.ask("hi")).text == "ok"
        assert calls == ["ui"]


def test_other_backends_ignore_the_ui_switch(router):
    r, calls = router({"ui_automation_enabled": False}, chain_backend="api")
    assert asyncio.run(r.ask("hi")).text == "ok"


def test_verbose_telemetry_logs_successful_calls(router, caplog):
    r, _ = router({"verbose_telemetry": True})
    with caplog.at_level(logging.INFO, logger="bot.router"):
        asyncio.run(r.ask("hi"))
    assert any("api/m1 answered in" in m and "7 tokens" in m for m in caplog.messages)


def test_quiet_without_verbose_telemetry(router, caplog):
    r, _ = router({"verbose_telemetry": False})
    with caplog.at_level(logging.INFO, logger="bot.router"):
        asyncio.run(r.ask("hi"))
    assert not any("answered in" in m for m in caplog.messages)
