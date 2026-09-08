"""AnthropicTransport's prompt-caching support (Phase A of the Claude
API/Claude Code parity plan) — explicit cache_control breakpoints on the
system prompt and the last tool schema, and cache usage telemetry
threaded back through NormalizedResponse. Confirmed live against
platform.claude.com/docs: a bare {"type": "ephemeral"} is Anthropic's own
5-minute default TTL shape; "ttl": "1h" is only sent for the explicit
1-hour opt-in.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _response(text="ok", cache_creation=None, cache_read=None):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read,
    )
    return SimpleNamespace(content=[_text_block(text)], stop_reason="end_turn", usage=usage)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _install(monkeypatch, responses):
    fake_messages = _FakeMessages(responses)
    fake_client = SimpleNamespace(messages=fake_messages)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: fake_client)
    return fake_messages


@pytest.fixture(autouse=True)
def _default_caching_config(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": True, "ttl": "5m"}}})
    yield


def _transport():
    return AnthropicTransport(api_key="sk-test")


def test_system_prompt_gets_a_cache_control_breakpoint(monkeypatch):
    fake = _install(monkeypatch, [_response()])

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10, system_prompt="you are a helpful assistant",
    ))

    system = fake.requests[0]["system"]
    assert system == [{"type": "text", "text": "you are a helpful assistant", "cache_control": {"type": "ephemeral"}}]


def test_last_tool_schema_gets_the_breakpoint_not_the_others(monkeypatch):
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "a", "description": "", "input_schema": {}}, {"name": "b", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10,
    ))

    sent_tools = fake.requests[0]["tools"]
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[1]["cache_control"] == {"type": "ephemeral"}
    # The original schema list/dicts must never be mutated in place —
    # they're the same shared objects every other call/transport reuses.
    assert "cache_control" not in tools[1]


def test_one_hour_ttl_is_sent_explicitly(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": True, "ttl": "1h"}}})
    fake = _install(monkeypatch, [_response()])

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10, system_prompt="sys",
    ))

    assert fake.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_disabled_falls_back_to_todays_plain_shapes(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": False}}})
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "a", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10, system_prompt="sys",
    ))

    assert fake.requests[0]["system"] == "sys"
    assert fake.requests[0]["tools"] == tools
    assert "cache_control" not in fake.requests[0]["tools"][0]


def test_no_system_prompt_or_tools_means_no_cache_control_anywhere(monkeypatch):
    fake = _install(monkeypatch, [_response()])

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10,
    ))

    assert "system" not in fake.requests[0]
    assert "tools" not in fake.requests[0]


def test_cache_usage_is_threaded_into_the_normalized_response(monkeypatch):
    _install(monkeypatch, [_response(cache_creation=1370, cache_read=0)])

    result = _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10, system_prompt="sys",
    ))

    assert result.cache_creation_tokens == 1370
    assert result.cache_read_tokens == 0


def test_missing_usage_cache_fields_stay_none_not_zero(monkeypatch):
    """A response shape that doesn't report cache fields at all (e.g. an
    older SDK/fixture) must not be misreported as "zero cache activity" —
    None means "unknown," not "confirmed no caching happened.\""""
    class _BareUsage:
        input_tokens = 10
        output_tokens = 5

    _install(monkeypatch, [SimpleNamespace(content=[_text_block("ok")], stop_reason="end_turn", usage=_BareUsage())])

    result = _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10,
    ))

    assert result.cache_creation_tokens is None
    assert result.cache_read_tokens is None
