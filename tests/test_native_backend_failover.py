"""NativeAgentBackend.ask()'s one-bounded-retry failover against a
configured fallback_provider/fallback_model (bot/agent_settings.py) —
mirrors Hermes's own real try_activate_fallback concept at a
deliberately bounded (one hop) scope, matching this codebase's existing
"one bounded retry" convention (output_schema validation works the same
way).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import agent_settings, bot_instances, providers
from bot.backends.api_backend import ApiBackend
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1],
    )


class _FailingAnthropicMessages:
    async def create(self, **kwargs):
        raise RuntimeError("simulated Anthropic outage")


def _install_failing_anthropic(monkeypatch):
    fake_client = SimpleNamespace(messages=_FailingAnthropicMessages())
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


class _FakeFallbackResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeFallbackAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json})
        return _FakeFallbackResponse(self._responses.pop(0))


def _install_fallback_http(monkeypatch, responses):
    fake = _FakeFallbackAsyncClient(responses)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def test_falls_back_on_primary_failure(temp_db, monkeypatch, tmp_path):
    _install_failing_anthropic(monkeypatch)
    providers.set_provider("fallback_provider", base_url="https://fallback.example/v1", api_key="sk-fallback")
    fake_http = _install_fallback_http(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "answered by the fallback"}}]},
    ])
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, fallback_provider="fallback_provider", fallback_model="fallback-model")

    result = _run(ApiBackend().ask(
        "hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id},
    ))

    assert result.text == "answered by the fallback"
    assert fake_http.requests[0]["json"]["model"] == "fallback-model"


def test_no_fallback_configured_raises_the_original_error(temp_db, monkeypatch, tmp_path):
    _install_failing_anthropic(monkeypatch)
    instance_id = _make_instance()

    with pytest.raises(BackendError, match="simulated Anthropic outage"):
        _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))


def test_fallback_itself_failing_raises_the_fallback_error(temp_db, monkeypatch, tmp_path):
    _install_failing_anthropic(monkeypatch)
    providers.set_provider("fallback_provider", base_url="https://fallback.example/v1", api_key="sk-fallback")
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, fallback_provider="fallback_provider", fallback_model="fallback-model")

    import httpx

    class _AlsoFailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("fallback also down")

    monkeypatch.setattr(
        "bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: _AlsoFailingClient()
    )

    with pytest.raises(BackendError, match="fallback also down"):
        _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))


def test_fallback_stays_active_for_the_rest_of_the_turn(temp_db, monkeypatch, tmp_path):
    """A second tool-call iteration after a successful fallback must not
    retry the (still-down) primary again — the fallback stays active for
    the rest of this ask() call."""
    _install_failing_anthropic(monkeypatch)
    providers.set_provider("fallback_provider", base_url="https://fallback.example/v1", api_key="sk-fallback")
    fake_http = _install_fallback_http(monkeypatch, [
        {
            "choices": [{
                "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}],
                },
            }],
        },
        {"choices": [{"message": {"role": "assistant", "content": "done, second call also via fallback"}}]},
    ])
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, fallback_provider="fallback_provider", fallback_model="fallback-model")

    result = _run(ApiBackend().ask(
        "list files", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id},
    ))

    assert result.text == "done, second call also via fallback"
    assert len(fake_http.requests) == 2
