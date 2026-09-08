"""AnthropicTransport's thinking-block preservation (Phase B of the
Claude API/Claude Code parity plan) — a real correctness bug found by
reading the code: _serialize_blocks() used to drop a thinking block's
real `thinking`/`signature` (or a redacted_thinking block's `data`) down
to a content-free `{"type": btype}` stub, which the API rejects on
replay for a thinking-enabled multi-turn tool-use conversation. Real
field names (signature, thinking, type / data, type) confirmed directly
against the installed anthropic SDK's ThinkingBlock/RedactedThinkingBlock
before writing this fix.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime.transports.anthropic import AnthropicTransport, _serialize_blocks
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _thinking_block(text, signature="sig-abc123"):
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def _redacted_thinking_block(data="opaque-redacted-payload"):
    return SimpleNamespace(type="redacted_thinking", data=data)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def test_serialize_blocks_preserves_a_real_thinking_block():
    out = _serialize_blocks([_thinking_block("reasoning about the answer", signature="sig-xyz")])
    assert out == [{"type": "thinking", "thinking": "reasoning about the answer", "signature": "sig-xyz"}]


def test_serialize_blocks_preserves_a_redacted_thinking_block():
    out = _serialize_blocks([_redacted_thinking_block("opaque-data")])
    assert out == [{"type": "redacted_thinking", "data": "opaque-data"}]


def test_serialize_blocks_mixed_thinking_and_tool_use():
    out = _serialize_blocks([
        _thinking_block("let me check the file", signature="sig-1"),
        _tool_use_block("tu_1", "list_dir", {"path": "."}),
    ])
    assert out == [
        {"type": "thinking", "thinking": "let me check the file", "signature": "sig-1"},
        {"type": "tool_use", "id": "tu_1", "name": "list_dir", "input": {"path": "."}},
    ]


def test_serialize_blocks_still_falls_back_for_a_genuinely_unknown_type():
    unknown = SimpleNamespace(type="some_future_block_type")
    assert _serialize_blocks([unknown]) == [{"type": "some_future_block_type"}]


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


def _response(blocks):
    return SimpleNamespace(content=blocks, stop_reason="end_turn", usage=None)


def test_thinking_text_never_leaks_into_the_visible_reply_text(monkeypatch):
    _install(monkeypatch, [_response([
        _thinking_block("internal reasoning nobody should see as the reply"),
        _text_block("the actual answer"),
    ])])

    result = _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=100, timeout_s=10,
    ))

    assert result.text == "the actual answer"


def test_thinking_summary_is_populated_and_capped(monkeypatch):
    from bot.agent_runtime.transports import anthropic as anthropic_transport

    long_thinking = "x" * 1000
    _install(monkeypatch, [_response([_thinking_block(long_thinking), _text_block("answer")])])

    result = _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=100, timeout_s=10,
    ))

    assert result.thinking_summary == long_thinking[:anthropic_transport.THINKING_SUMMARY_MAX_CHARS]
    assert len(result.thinking_summary) == anthropic_transport.THINKING_SUMMARY_MAX_CHARS


def test_no_thinking_block_means_no_summary(monkeypatch):
    _install(monkeypatch, [_response([_text_block("answer")])])

    result = _run(AnthropicTransport(api_key="sk-test").send(
        model="claude-sonnet-5", history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=100, timeout_s=10,
    ))

    assert result.thinking_summary is None
