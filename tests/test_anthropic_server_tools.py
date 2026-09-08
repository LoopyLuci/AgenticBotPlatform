"""bot/agent_runtime/anthropic_server_tools.py — Anthropic server-executed
tools (Phase D of the Claude API/Claude Code parity plan). Server tool
call/result blocks are structurally distinct from client tool_use blocks
(a "server_tool_use" type, confirmed live against Anthropic's own
documented example response), so no dispatch code is needed anywhere —
these tests cover the registry's config-driven opt-in and its wiring
into AnthropicTransport.send()'s tools array.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime import anthropic_server_tools
from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def test_all_off_by_default():
    assert anthropic_server_tools.enabled_tool_entries() == []


def test_enabling_one_tool_returns_only_that_entry(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"server_tools": {"web_search": True}}})
    assert anthropic_server_tools.enabled_tool_entries() == [{"type": "web_search_20260318", "name": "web_search"}]


def test_enabling_multiple_tools_returns_all_of_them(monkeypatch):
    monkeypatch.setattr(
        config, "_data",
        {**config._data, "native_agent": {"server_tools": {"web_search": True, "code_execution": True}}},
    )
    entries = anthropic_server_tools.enabled_tool_entries()
    names = {e["name"] for e in entries}
    assert names == {"web_search", "code_execution"}


def test_unknown_config_key_is_ignored_not_raised(monkeypatch):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"server_tools": {"not_a_real_tool": True}}})
    assert anthropic_server_tools.enabled_tool_entries() == []


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


def _response(text="ok"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=None)


@pytest.fixture(autouse=True)
def _no_prompt_caching(monkeypatch):
    # Isolates these tests from Phase A's cache_control breakpoint, which
    # would otherwise land on whichever tool ends up last and complicate
    # the plain equality assertions below.
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"prompt_caching": {"enabled": False}}})
    yield


def test_send_appends_no_server_tools_when_none_enabled(monkeypatch):
    fake = _install(monkeypatch, [_response()])

    _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=100, timeout_s=10,
    ))

    assert "tools" not in fake.requests[0]


def test_send_appends_an_enabled_server_tool_alongside_client_tools(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        **config._data,
        "native_agent": {"server_tools": {"web_search": True}, "prompt_caching": {"enabled": False}},
    })
    fake = _install(monkeypatch, [_response()])
    client_tools = [{"name": "run_shell", "description": "", "input_schema": {}}]

    _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=client_tools,
        max_tokens=100, timeout_s=10,
    ))

    sent_tools = fake.requests[0]["tools"]
    # Phase H adds strict:true to BotServer's own client tool schemas —
    # the server tool entry below must NOT get it.
    assert {"name": "run_shell", "description": "", "input_schema": {}, "strict": True} in sent_tools
    assert {"type": "web_search_20260318", "name": "web_search"} in sent_tools


def test_a_server_tool_use_block_never_becomes_a_local_tool_call(monkeypatch):
    """The real distinguishing fact this whole phase relies on: a server
    tool's call block is type "server_tool_use", not "tool_use" — so
    NativeAgentBackend's tool_calls extraction (which filters on
    type=="tool_use" exactly) never mistakes it for something
    tool_loop.run_one_tool() needs to execute locally."""
    server_tool_use_block = SimpleNamespace(
        type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "test"},
    )
    text_block = SimpleNamespace(type="text", text="the answer")
    _install(monkeypatch, [SimpleNamespace(content=[server_tool_use_block, text_block], stop_reason="end_turn", usage=None)])
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"server_tools": {"web_search": True}}})

    result = _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}],
        tool_schemas=[{"name": "run_shell", "description": "", "input_schema": {}}], max_tokens=100, timeout_s=10,
    ))

    assert result.tool_calls == []
    assert result.text == "the answer"


class _FakePydanticBlock:
    """Mimics a real anthropic SDK block enough to exercise
    _serialize_blocks()'s generic model_dump() fallback (server_tool_use
    and every *_tool_result type) without needing the real SDK classes."""

    def __init__(self, type_, **fields):
        self.type = type_
        self._fields = fields

    def model_dump(self, mode="json", exclude_none=True):
        return {"type": self.type, **self._fields}


def test_serialize_blocks_preserves_a_server_tool_use_block_via_model_dump():
    from bot.agent_runtime.transports.anthropic import _serialize_blocks

    block = _FakePydanticBlock("server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "claude shannon"})
    out = _serialize_blocks([block])
    assert out == [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "claude shannon"}}]


def test_serialize_blocks_preserves_a_tool_result_block_via_model_dump():
    from bot.agent_runtime.transports.anthropic import _serialize_blocks

    block = _FakePydanticBlock(
        "web_search_tool_result", tool_use_id="srvtoolu_1",
        content=[{"type": "web_search_result", "url": "https://example.com", "encrypted_content": "abc"}],
    )
    out = _serialize_blocks([block])
    assert out[0]["type"] == "web_search_tool_result"
    assert out[0]["tool_use_id"] == "srvtoolu_1"
    assert out[0]["content"][0]["encrypted_content"] == "abc"
