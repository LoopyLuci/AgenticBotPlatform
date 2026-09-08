"""bot/agent_runtime/compression.py — the character-count heuristic that
summarizes older turns into one digest before a long session's history
grows unbounded. Uses a fake transport (no real network) exercising the
real bot.db round trip via temp_db, mirroring this project's existing
fake-transport test patterns.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import db
from bot.agent_runtime import compression
from bot.agent_runtime.transports.base import NormalizedResponse


def _run(coro):
    return asyncio.run(coro)


class _FakeTransport:
    def __init__(self, digest_text="a compact summary"):
        self.digest_text = digest_text
        self.send_calls: list[dict] = []

    def user_message(self, text, *, images=None):
        return {"role": "user", "content": text}

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.send_calls.append({"history": history, "max_tokens": max_tokens, "tool_schemas": tool_schemas})
        return NormalizedResponse(text=self.digest_text, assistant_message={"role": "assistant", "content": self.digest_text})


def _seed_history(session_key: str, n: int, text: str = "x") -> None:
    for i in range(n):
        db.append_agent_message(session_key, "user" if i % 2 == 0 else "assistant", f"{text}-{i}")


def test_below_threshold_does_not_compress(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 10_000)
    _seed_history("s1", 10, text="short")
    transport = _FakeTransport()

    compressed = _run(compression.maybe_compress("s1", transport, model="m"))

    assert compressed is False
    assert transport.send_calls == []
    assert len(db.list_agent_messages("s1")) == 10


def test_too_few_messages_never_compresses_even_if_individually_huge(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 10)
    _seed_history("s1", compression.KEEP_LAST_N_MESSAGES, text="x" * 100)
    transport = _FakeTransport()

    compressed = _run(compression.maybe_compress("s1", transport, model="m"))

    assert compressed is False


def test_zero_threshold_disables_compression(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 0)
    _seed_history("s1", 50, text="x" * 1000)
    transport = _FakeTransport()

    compressed = _run(compression.maybe_compress("s1", transport, model="m"))

    assert compressed is False
    assert transport.send_calls == []


def test_over_threshold_compresses_and_keeps_recent_messages_verbatim(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 100)
    _seed_history("s1", 20, text="msg")  # 20 short messages still add up past 100 chars total
    transport = _FakeTransport(digest_text="digest of the old stuff")

    compressed = _run(compression.maybe_compress("s1", transport, model="m"))

    assert compressed is True
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["tool_schemas"] == []  # no tool loop for the digest call

    remaining = db.list_agent_messages("s1")
    assert len(remaining) == compression.KEEP_LAST_N_MESSAGES + 1  # digest + kept tail
    assert "digest of the old stuff" in remaining[0]["content"]
    # The kept tail is exactly the last N original rows, in original order.
    assert remaining[-1]["content"] == "msg-19"
    assert remaining[1]["content"] == f"msg-{20 - compression.KEEP_LAST_N_MESSAGES}"


def test_fires_at_most_once_a_second_call_right_after_does_nothing_more(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 100)
    _seed_history("s1", 20, text="msg")
    transport = _FakeTransport(digest_text="short digest")

    _run(compression.maybe_compress("s1", transport, model="m"))
    second = _run(compression.maybe_compress("s1", transport, model="m"))

    # The post-compression history (one short digest + the short kept
    # tail) is back under threshold, so a second immediate call no-ops.
    assert second is False
    assert len(transport.send_calls) == 1


def test_digest_call_failure_leaves_history_uncompressed_and_swallows_the_error(temp_db, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 100)
    _seed_history("s1", 20, text="x" * 50)

    class _FailingTransport(_FakeTransport):
        async def send(self, **kwargs):
            raise RuntimeError("network is down")

    compressed = _run(compression.maybe_compress("s1", _FailingTransport(), model="m"))

    assert compressed is False
    assert len(db.list_agent_messages("s1")) == 20


def test_db_compress_agent_messages_with_keep_last_n_zero(temp_db):
    _seed_history("s1", 5, text="msg")

    db.compress_agent_messages("s1", keep_last_n=0, digest_role="user", digest_content="everything summarized")

    remaining = db.list_agent_messages("s1")
    assert len(remaining) == 1
    assert remaining[0]["content"] == "everything summarized"


def test_flatten_handles_every_real_stored_content_shape():
    assert compression._flatten("plain string") == "plain string"
    assert compression._flatten([{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "list_dir"}]) == "hi [tool call: list_dir]"
    assert compression._flatten({"content": "nested"}) == "nested"
    assert compression._flatten({"output": [{"type": "message", "content": [{"type": "output_text", "text": "hey"}]}]}) == "hey"
    assert compression._flatten(None) == ""
