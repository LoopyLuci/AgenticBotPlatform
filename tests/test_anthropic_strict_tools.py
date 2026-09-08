"""AnthropicTransport.send() adds strict: true to BotServer's own client
tool schemas (Phase H, the last phase of the Claude API/Claude Code
parity plan) — free schema-conformance tightening. Anthropic's own
server tool entries (web_search/web_fetch/code_execution/tool_search,
Phase D) must NOT get it, since the tool-reference page's strict-support
scoping doesn't cover those the same way it covers a plain client tool.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _response(text="ok"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=None)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _install(monkeypatch, responses):
    fake_messages = _FakeMessages(responses)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: SimpleNamespace(messages=fake_messages))
    return fake_messages


@pytest.fixture(autouse=True)
def _no_prompt_caching(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": False}}})
    yield


def _transport():
    return AnthropicTransport(api_key="sk-test")


def test_client_tool_schemas_get_strict_true(monkeypatch):
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "run_shell", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10,
    ))

    sent = fake.requests[0]["tools"]
    assert sent == [{"name": "run_shell", "description": "", "input_schema": {}, "strict": True}]


def test_original_schema_dicts_are_never_mutated(monkeypatch):
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "run_shell", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10,
    ))

    assert "strict" not in tools[0]


def test_server_tool_entries_never_get_strict(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        **config._data,
        "native_agent": {"server_tools": {"web_search": True}, "prompt_caching": {"enabled": False}},
    })
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "run_shell", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10,
    ))

    sent = fake.requests[0]["tools"]
    client_entry = next(t for t in sent if t.get("name") == "run_shell")
    server_entry = next(t for t in sent if t.get("type") == "web_search_20260318")
    assert client_entry["strict"] is True
    assert "strict" not in server_entry


def test_no_tool_schemas_and_no_server_tools_means_no_tools_key(monkeypatch):
    fake = _install(monkeypatch, [_response()])

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[],
        max_tokens=100, timeout_s=10,
    ))

    assert "tools" not in fake.requests[0]


def test_strict_composes_correctly_with_cache_control_on_last_tool(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": True, "ttl": "5m"}}})
    fake = _install(monkeypatch, [_response()])
    tools = [{"name": "a", "description": "", "input_schema": {}}, {"name": "b", "description": "", "input_schema": {}}]

    _run(_transport().send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=tools,
        max_tokens=100, timeout_s=10,
    ))

    sent = fake.requests[0]["tools"]
    assert sent[0]["strict"] is True
    assert "cache_control" not in sent[0]
    assert sent[1]["strict"] is True
    assert sent[1]["cache_control"] == {"type": "ephemeral"}
