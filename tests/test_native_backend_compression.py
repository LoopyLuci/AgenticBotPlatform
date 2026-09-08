"""NativeAgentBackend.ask()'s wiring of compression.maybe_compress() —
confirms the real ask() call path actually triggers a compression digest
call when a session's stored history crosses the configured threshold,
using CustomModelBackend's real OpenAICompatibleTransport with the
outbound HTTP faked (same pattern as test_custom_model_backend.py).
"""
from __future__ import annotations

import asyncio

from bot import db
from bot.agent_runtime import compression
from bot.backends.custom_model_backend import CustomModelBackend


def _run(coro):
    return asyncio.run(coro)


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
        self.requests.append(json)
        return _FakeResponse(self._responses.pop(0))


def _install(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def _backend():
    return CustomModelBackend(provider_name="local_ollama", model_id="llama3.1", base_url="http://127.0.0.1:11434/v1")


def test_a_long_session_gets_compressed_before_the_real_turn(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 100)
    session_key = "custom-preexisting"
    for i in range(20):
        db.append_agent_message(session_key, "user" if i % 2 == 0 else "assistant", f"old message {i}")

    fake = _install(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "a compact digest"}}]},  # the compression call
        {"choices": [{"message": {"role": "assistant", "content": "real answer"}}]},  # the actual turn
    ])

    result = _run(_backend().ask(
        "what's next?", context={"cwd": str(tmp_path / "ws"), "desktop_session_key": session_key},
    ))

    assert result.text == "real answer"
    assert len(fake.requests) == 2
    # First request is the digest call: no tools, small max_tokens.
    assert "tools" not in fake.requests[0]
    assert fake.requests[0]["max_tokens"] == compression.DIGEST_MAX_TOKENS
    stored = db.list_agent_messages(session_key)
    assert any("a compact digest" in (m["content"] if isinstance(m["content"], str) else "") for m in stored)


def test_a_short_session_never_triggers_a_compression_call(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 60_000)
    session_key = "custom-short"
    db.append_agent_message(session_key, "user", "hi")
    db.append_agent_message(session_key, "assistant", "hello")

    fake = _install(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "real answer"}}]},
    ])

    _run(_backend().ask("continue", context={"cwd": str(tmp_path / "ws"), "desktop_session_key": session_key}))

    assert len(fake.requests) == 1
