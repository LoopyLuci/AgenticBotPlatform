"""bot/agent_runtime/output_schema.py — validates a spawn_subagent
child's final answer against a caller-supplied JSON Schema, with exactly
one bounded retry on failure (ported from the real Hermes Agent's own
delegation_output_schema.py design)."""

from __future__ import annotations

import asyncio

from bot.agent_runtime.output_schema import validate_or_retry
from bot.backends.base import BackendResult

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def _run(coro):
    return asyncio.run(coro)


class _FakeBackend:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.calls += 1
        return BackendResult(text=self._replies.pop(0), tokens=None, raw=None)


def test_valid_json_passes_immediately_no_retry():
    backend = _FakeBackend([])
    ok, result = _run(validate_or_retry(backend, '{"answer": "42"}', SCHEMA, context={}, timeout_s=30))
    assert ok
    assert result == '{"answer": "42"}'
    assert backend.calls == 0


def test_invalid_json_retries_once_then_succeeds():
    backend = _FakeBackend(['{"answer": "fixed"}'])
    ok, result = _run(validate_or_retry(backend, "not json at all", SCHEMA, context={}, timeout_s=30))
    assert ok
    assert result == '{"answer": "fixed"}'
    assert backend.calls == 1


def test_schema_mismatch_retries_once_then_succeeds():
    backend = _FakeBackend(['{"answer": "now correct"}'])
    ok, result = _run(validate_or_retry(backend, '{"wrong_field": 1}', SCHEMA, context={}, timeout_s=30))
    assert ok
    assert backend.calls == 1


def test_still_invalid_after_one_retry_gives_up():
    backend = _FakeBackend(["still not json"])
    ok, result = _run(validate_or_retry(backend, "not json", SCHEMA, context={}, timeout_s=30))
    assert not ok
    assert "validation failed" in result
    assert backend.calls == 1  # exactly one retry, never more


def test_malformed_caller_schema_fails_without_ever_retrying():
    backend = _FakeBackend([])
    bad_schema = {"type": "not-a-real-type"}
    ok, result = _run(validate_or_retry(backend, '{"x": 1}', bad_schema, context={}, timeout_s=30))
    assert not ok
    assert backend.calls == 0
