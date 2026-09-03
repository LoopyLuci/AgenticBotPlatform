"""bot/backends/api_backend.py — the direct-Anthropic backend. No test
file existed for this before the Native Hermes-parity refactor (a real,
previously-uncaught coverage gap — only custom_model_backend.py had
tests exercising the shared tool-use loop). Exercises the real tool-use
loop (bot/agent_runtime's tools/tool_loop, against a real temp workspace
and temp_db) with the Anthropic SDK call faked, mirroring
test_custom_model_backend.py's approach for the OpenAI-compatible side.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.backends.api_backend import ApiBackend
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _response(blocks, stop_reason="end_turn", usage=(5, 3)):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]) if usage else None,
    )


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _install_fake_client(monkeypatch, responses):
    fake_messages = _FakeMessages(responses)
    fake_client = SimpleNamespace(messages=fake_messages)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return fake_messages


def test_simple_reply_with_no_tool_calls(temp_db, monkeypatch, tmp_path):
    _install_fake_client(monkeypatch, [_response([_text_block("Hello from Claude!")])])
    result = _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws")}))
    assert result.text == "Hello from Claude!"
    assert result.tokens == 8
    assert result.raw["desktop_session_key"].startswith("api-")


def test_existing_session_is_reused_not_recreated(temp_db, monkeypatch, tmp_path):
    _install_fake_client(monkeypatch, [_response([_text_block("ok")])])
    result = _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws"), "desktop_session_key": "api-existing"}))
    assert "desktop_session_key" not in result.raw


def test_safe_tool_call_round_trips(temp_db, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")

    fake_messages = _install_fake_client(monkeypatch, [
        _response([_tool_use_block("tu_1", "list_dir", {"path": "."})], stop_reason="tool_use"),
        _response([_text_block("Found hello.txt")]),
    ])
    result = _run(ApiBackend().ask("what's in the workspace?", context={"cwd": str(workspace)}))
    assert result.text == "Found hello.txt"
    # Second request's messages must include the tool's real output, not a
    # stub — proves execute_tool() actually ran against the real filesystem.
    second_request_messages = fake_messages.requests[1]["messages"]
    tool_result_msgs = [m for m in second_request_messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert any(
        block.get("type") == "tool_result" and "hello.txt" in block.get("content", "")
        for m in tool_result_msgs for block in m["content"]
    )


def test_missing_api_key_raises_backend_error(temp_db, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BackendError):
        _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws")}))


def test_sdk_error_raises_backend_error(temp_db, monkeypatch, tmp_path):
    class _FailingMessages:
        async def create(self, **kwargs):
            raise RuntimeError("boom")

    fake_client = SimpleNamespace(messages=_FailingMessages())
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(BackendError):
        _run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws")}))
