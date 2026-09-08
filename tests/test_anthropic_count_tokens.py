"""AnthropicTransport.count_tokens() (Phase F of the Claude API/Claude
Code parity plan) — the real, free client.messages.count_tokens()
endpoint, confirmed against the installed anthropic SDK's actual method
signature and MessageTokensCount.input_tokens field name before writing
this.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


class _FakeMessages:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _install(monkeypatch, result):
    fake_messages = _FakeMessages(result)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: SimpleNamespace(messages=fake_messages))
    return fake_messages


def test_count_tokens_returns_the_real_input_token_count(monkeypatch):
    fake = _install(monkeypatch, SimpleNamespace(input_tokens=1234))

    result = _run(AnthropicTransport(api_key="sk-test").count_tokens(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}],
    ))

    assert result == 1234
    assert fake.calls[0]["model"] == "claude-sonnet-5"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in fake.calls[0]
    assert "system" not in fake.calls[0]


def test_count_tokens_includes_tools_and_system_when_given(monkeypatch):
    fake = _install(monkeypatch, SimpleNamespace(input_tokens=1234))
    tools = [{"name": "run_shell", "description": "", "input_schema": {}}]

    _run(AnthropicTransport(api_key="sk-test").count_tokens(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}],
        tool_schemas=tools, system_prompt="be helpful",
    ))

    assert fake.calls[0]["tools"] == tools
    assert fake.calls[0]["system"] == "be helpful"


def test_count_tokens_wraps_a_failure_as_backend_error(monkeypatch):
    class _FailingMessages:
        async def count_tokens(self, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: SimpleNamespace(messages=_FailingMessages()))

    with pytest.raises(BackendError, match="network down"):
        _run(AnthropicTransport(api_key="sk-test").count_tokens(model="claude-sonnet-5", history=[]))
