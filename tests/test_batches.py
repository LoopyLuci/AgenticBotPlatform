"""bot/agent_runtime/batches.py — the Anthropic Message Batches API
wrapper (Phase F of the Claude API/Claude Code parity plan). Faked at
the real SDK boundary (client.messages.batches.create/retrieve/results),
matching this project's own established fake-client testing pattern.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.agent_runtime import batches
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


class _FakeBatch:
    def __init__(self, id_, processing_status="in_progress", counts=None):
        self.id = id_
        self.processing_status = processing_status
        counts = counts or {}
        self.request_counts = SimpleNamespace(
            processing=counts.get("processing", 1), succeeded=counts.get("succeeded", 0),
            errored=counts.get("errored", 0), canceled=counts.get("canceled", 0), expired=counts.get("expired", 0),
        )


class _FakeBatches:
    def __init__(self, create_result=None, retrieve_result=None, results_items=None):
        self._create_result = create_result
        self._retrieve_result = retrieve_result
        self._results_items = results_items or []
        self.create_calls = []

    async def create(self, *, requests):
        self.create_calls.append(requests)
        return self._create_result

    async def retrieve(self, batch_id):
        return self._retrieve_result

    async def results(self, batch_id):
        items = self._results_items

        async def _gen():
            for item in items:
                yield item

        return _gen()


def _install(monkeypatch, fake_batches):
    fake_client = SimpleNamespace(messages=SimpleNamespace(batches=fake_batches))
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def test_submit_builds_real_batch_requests_and_returns_the_batch_id(monkeypatch):
    fake_batches = _FakeBatches(create_result=_FakeBatch("msgbatch_123"))
    _install(monkeypatch, fake_batches)

    batch_id = _run(batches.submit(
        [{"custom_id": "t1", "goal": "summarize this"}, {"custom_id": "t2", "goal": "classify that"}],
        model="claude-sonnet-5", system_prompt="be concise",
    ))

    assert batch_id == "msgbatch_123"
    sent = fake_batches.create_calls[0]
    assert sent[0]["custom_id"] == "t1"
    assert sent[0]["params"]["messages"] == [{"role": "user", "content": "summarize this"}]
    assert sent[0]["params"]["system"] == "be concise"
    assert sent[0]["params"]["model"] == "claude-sonnet-5"


def test_submit_rejects_an_empty_task_list(monkeypatch):
    _install(monkeypatch, _FakeBatches())
    with pytest.raises(BackendError, match="at least one task"):
        _run(batches.submit([], model="claude-sonnet-5"))


def test_submit_rejects_more_than_the_real_anthropic_cap(monkeypatch):
    _install(monkeypatch, _FakeBatches())
    too_many = [{"custom_id": str(i), "goal": "x"} for i in range(batches.MAX_BATCH_REQUESTS + 1)]
    with pytest.raises(BackendError, match="exceeds Anthropic's own"):
        _run(batches.submit(too_many, model="claude-sonnet-5"))


def test_status_reports_processing_status_and_counts(monkeypatch):
    fake_batches = _FakeBatches(retrieve_result=_FakeBatch(
        "msgbatch_123", processing_status="ended", counts={"succeeded": 2, "errored": 1},
    ))
    _install(monkeypatch, fake_batches)

    result = _run(batches.status("msgbatch_123"))

    assert result["processing_status"] == "ended"
    assert result["request_counts"]["succeeded"] == 2
    assert result["request_counts"]["errored"] == 1


def _succeeded_item(custom_id, text):
    message = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])
    result = SimpleNamespace(type="succeeded", message=message)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _errored_item(custom_id, error):
    result = SimpleNamespace(type="errored", error=error)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _expired_item(custom_id):
    result = SimpleNamespace(type="expired")
    return SimpleNamespace(custom_id=custom_id, result=result)


def test_results_extracts_text_from_succeeded_items(monkeypatch):
    fake_batches = _FakeBatches(results_items=[_succeeded_item("t1", "the summary")])
    _install(monkeypatch, fake_batches)

    result = _run(batches.results("msgbatch_123"))

    assert result == [{"custom_id": "t1", "status": "succeeded", "text": "the summary", "error": None}]


def test_results_extracts_errors_from_errored_items(monkeypatch):
    fake_batches = _FakeBatches(results_items=[_errored_item("t1", "rate limited")])
    _install(monkeypatch, fake_batches)

    result = _run(batches.results("msgbatch_123"))

    assert result[0]["status"] == "errored"
    assert result[0]["text"] is None
    assert "rate limited" in result[0]["error"]


def test_results_handles_expired_items_without_a_message_or_error(monkeypatch):
    fake_batches = _FakeBatches(results_items=[_expired_item("t1")])
    _install(monkeypatch, fake_batches)

    result = _run(batches.results("msgbatch_123"))

    assert result == [{"custom_id": "t1", "status": "expired", "text": None, "error": None}]


def test_missing_api_key_raises_backend_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BackendError, match="ANTHROPIC_API_KEY"):
        _run(batches.submit([{"custom_id": "t1", "goal": "x"}], model="claude-sonnet-5"))
