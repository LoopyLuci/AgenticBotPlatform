"""bot/agent_runtime/transports/responses_api.py — the OpenAI Responses
API (/v1/responses) transport, structurally distinct from the
chat-completions shape openai_compatible.py speaks. Mirrors
test_transports.py's existing fake-HTTP-boundary style.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime.transports import build_openai_transport
from bot.agent_runtime.transports.responses_api import ResponsesApiTransport, to_responses_tools
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, data, status_code=200):
        self._data = data
        self._status_code = status_code
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._status_code, self._data)


def _install(monkeypatch, data, status_code=200):
    fake = _FakeAsyncClient(data, status_code)
    monkeypatch.setattr("bot.agent_runtime.transports.responses_api.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def test_to_responses_tools_is_flat_not_nested():
    tools = to_responses_tools([{"name": "read_file", "description": "reads", "input_schema": {"type": "object"}}])
    assert tools == [{"type": "function", "name": "read_file", "description": "reads", "parameters": {"type": "object"}}]


def test_build_openai_transport_selects_responses_api_by_protocol():
    t = build_openai_transport(protocol="responses", base_url="https://example.com/v1")
    assert isinstance(t, ResponsesApiTransport)


def test_build_openai_transport_defaults_to_chat_completions():
    from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport

    t = build_openai_transport(protocol="openai", base_url="https://example.com/v1")
    assert isinstance(t, OpenAICompatibleTransport)


def test_send_returns_plain_text_reply(monkeypatch):
    fake = _install(monkeypatch, {
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi there"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    transport = ResponsesApiTransport(base_url="https://example.com/v1", api_key="sk-test")

    result = _run(transport.send(
        model="o-test", history=[transport.user_message("hello")], tool_schemas=[], max_tokens=100, timeout_s=10,
    ))

    assert result.text == "hi there"
    assert result.tool_calls == []
    assert result.tokens == 15
    assert fake.requests[0]["url"] == "https://example.com/v1/responses"
    assert fake.requests[0]["json"]["input"][0]["content"][0]["text"] == "hello"


def test_send_extracts_a_function_call(monkeypatch):
    _install(monkeypatch, {
        "output": [{"type": "function_call", "call_id": "call_1", "name": "list_dir", "arguments": "{\"path\": \".\"}"}],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    })
    transport = ResponsesApiTransport(base_url="https://example.com/v1")

    result = _run(transport.send(
        model="o-test", history=[transport.user_message("list files")],
        tool_schemas=[{"name": "list_dir", "input_schema": {"type": "object"}}], max_tokens=100, timeout_s=10,
    ))

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "list_dir"
    assert result.tool_calls[0].arguments == {"path": "."}
    assert result.stop is False


def test_tool_result_round_trips_into_the_next_call(monkeypatch):
    fake = _install(monkeypatch, {
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
    })
    transport = ResponsesApiTransport(base_url="https://example.com/v1")
    from bot.agent_runtime.transports.base import ToolCall

    tool_result_entries = transport.tool_result_messages([(ToolCall(id="call_1", name="list_dir", arguments={}), "a.txt")])
    history = [transport.user_message("list files"), *tool_result_entries]

    _run(transport.send(model="o-test", history=history, tool_schemas=[], max_tokens=100, timeout_s=10))

    sent_input = fake.requests[0]["json"]["input"]
    function_call_outputs = [item for item in sent_input if item.get("type") == "function_call_output"]
    assert function_call_outputs == [{"type": "function_call_output", "call_id": "call_1", "output": "a.txt"}]


def test_no_output_raises_backend_error(monkeypatch):
    _install(monkeypatch, {"output": []})
    transport = ResponsesApiTransport(base_url="https://example.com/v1")

    with pytest.raises(BackendError, match="no output"):
        _run(transport.send(model="o-test", history=[transport.user_message("hi")], tool_schemas=[], max_tokens=100, timeout_s=10))


def test_http_error_raises_backend_error(monkeypatch):
    _install(monkeypatch, {"error": "bad request"}, status_code=400)
    transport = ResponsesApiTransport(base_url="https://example.com/v1")

    with pytest.raises(BackendError):
        _run(transport.send(model="o-test", history=[transport.user_message("hi")], tool_schemas=[], max_tokens=100, timeout_s=10))
