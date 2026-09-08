"""NativeAgentBackend.ask()'s thinking-summary progress surface (Phase B
of the Claude API/Claude Code parity plan) — gated behind
native_agent.show_thinking_summary, default off.
"""
from __future__ import annotations

import asyncio

from bot.config import config
from bot.agent_runtime.transports.base import NormalizedResponse


def _run(coro):
    return asyncio.run(coro)


class _FakeTransport:
    supports_vision = False

    def __init__(self, response):
        self._response = response

    def user_message(self, text, *, images=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return []

    async def send(self, **kwargs):
        return self._response


def _backend_with(response):
    from bot.backends.native_backend import NativeAgentBackend

    return NativeAgentBackend(_FakeTransport(response), model="claude-sonnet-5")


def test_thinking_summary_reaches_progress_notify_when_enabled(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"show_thinking_summary": True}})
    response = NormalizedResponse(text="the answer", assistant_message={"role": "assistant", "content": "the answer"}, thinking_summary="reasoning excerpt")
    backend = _backend_with(response)

    progress_calls = []

    async def _progress(text):
        progress_calls.append(text)

    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "progress_notify": _progress}))

    assert any("reasoning excerpt" in call for call in progress_calls)


def test_thinking_summary_does_not_fire_when_disabled(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"show_thinking_summary": False}})
    response = NormalizedResponse(text="the answer", assistant_message={"role": "assistant", "content": "the answer"}, thinking_summary="reasoning excerpt")
    backend = _backend_with(response)

    progress_calls = []

    async def _progress(text):
        progress_calls.append(text)

    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "progress_notify": _progress}))

    assert progress_calls == []


def test_no_progress_notify_channel_never_raises(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"show_thinking_summary": True}})
    response = NormalizedResponse(text="the answer", assistant_message={"role": "assistant", "content": "the answer"}, thinking_summary="reasoning excerpt")
    backend = _backend_with(response)

    result = _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws")}))

    assert result.text == "the answer"
