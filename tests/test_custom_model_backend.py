"""bot/backends/custom_model_backend.py — the "any OpenAI-compatible
endpoint" backend. Exercises the real tool-use loop (bot/agent_runtime's
tools/tool_loop, against a real temp workspace and temp_db) with the
outbound HTTP call faked, so this covers the actual response-shape
parsing and tool round-trip, not just a mocked-out method.
"""

from __future__ import annotations

import pytest

from bot.backends.base import BackendError
from bot.backends.custom_model_backend import CustomModelBackend


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._responses.pop(0))


def _install_fake_client(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)

    def _factory(*, timeout):
        return fake

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", _factory)
    return fake


def _backend():
    return CustomModelBackend(
        provider_name="local_ollama",
        model_id="llama3.1",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
    )


def test_simple_reply_with_no_tool_calls(temp_db, monkeypatch, tmp_path):
    _install_fake_client(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "Hello from a local model!"}}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    ])
    result = _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))
    assert result.text == "Hello from a local model!"
    assert result.tokens == 8
    assert result.raw["desktop_session_key"].startswith("custom-")


def test_new_session_is_created_when_none_given(temp_db, monkeypatch, tmp_path):
    _install_fake_client(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    result = _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))
    assert result.raw["desktop_session_key"].startswith("custom-")


def test_existing_session_is_reused_not_recreated(temp_db, monkeypatch, tmp_path):
    _install_fake_client(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    result = _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws"), "desktop_session_key": "custom-existing"}))
    assert "desktop_session_key" not in result.raw


def test_safe_tool_call_round_trips(temp_db, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")

    fake = _install_fake_client(monkeypatch, [
        {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "list_dir", "arguments": '{"path": "."}'}}],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "Found hello.txt"}}]},
    ])
    result = _run(_backend().ask("what's in the workspace?", context={"cwd": str(workspace)}))
    assert result.text == "Found hello.txt"
    # Second request's messages must include the tool's real output, not a
    # stub — proves execute_tool() actually ran against the real filesystem.
    second_request_messages = fake.requests[1]["json"]["messages"]
    tool_messages = [m for m in second_request_messages if m.get("role") == "tool"]
    assert any("hello.txt" in m["content"] for m in tool_messages)


def test_http_error_raises_backend_error(temp_db, monkeypatch, tmp_path):
    import httpx

    class _ErrorClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            request = httpx.Request("POST", url)
            response = httpx.Response(500, text="internal error", request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient",
                         lambda *, timeout: _ErrorClient([]))
    with pytest.raises(BackendError):
        _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))


def test_authorization_header_set_when_api_key_present(temp_db, monkeypatch, tmp_path):
    fake = _install_fake_client(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    backend = CustomModelBackend(
        provider_name="openrouter", model_id="some/model",
        base_url="https://openrouter.ai/api/v1", api_key="sk-test",
    )
    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws")}))
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer sk-test"


def _run(coro):
    import asyncio

    return asyncio.run(coro)
