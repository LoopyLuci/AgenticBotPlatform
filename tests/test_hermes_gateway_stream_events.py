"""Phase 9: HermesGatewayBackend.stream_tool_events() — the live top-level
SSE tap over the gateway's /api/sessions/{id}/chat/stream endpoint (a
different request path than every other method in this class, which
speaks the WS JSON-RPC protocol instead — see the module docstring).

Faked at the httpx boundary, matching test_hermes_model_discovery.py's
established pattern for this same class's other HTTP method
(fetch_model_options).
"""

from __future__ import annotations

import asyncio

import pytest

from bot.backends.hermes_gateway_backend import HermesGatewayBackend


def _run(coro):
    return asyncio.run(coro)


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamContext:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, lines, calls):
        self._lines = lines
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self._calls.append({"method": method, "url": url, "headers": headers})
        return _FakeStreamContext(self._lines)


def _patch_httpx(monkeypatch, lines):
    import httpx

    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(lines, calls))
    return calls


async def _collect(backend, session_id):
    return [event async for event in backend.stream_tool_events(session_id)]


def test_stream_tool_events_filters_to_delegate_task(monkeypatch):
    backend = HermesGatewayBackend(port=9999)
    lines = [
        "event: tool_started",
        'data: {"name": "delegate_task", "tool_call_id": "1"}',
        "",
        "event: tool_started",
        'data: {"name": "some_other_tool", "tool_call_id": "2"}',
        "",
        "event: tool_completed",
        'data: {"name": "delegate_task", "tool_call_id": "1"}',
        "",
    ]
    _patch_httpx(monkeypatch, lines)

    events = _run(_collect(backend, "sess-1"))

    assert [e["event"] for e in events] == ["tool_started", "tool_completed"]
    assert all(e["name"] == "delegate_task" for e in events)


def test_stream_tool_events_skips_malformed_data_lines(monkeypatch):
    backend = HermesGatewayBackend(port=9999)
    lines = [
        "event: tool_started",
        "data: not valid json",
        "event: tool_completed",
        'data: {"name": "delegate_task"}',
    ]
    _patch_httpx(monkeypatch, lines)

    events = _run(_collect(backend, "sess-1"))

    assert len(events) == 1
    assert events[0]["event"] == "tool_completed"


def test_stream_tool_events_uses_header_auth_not_query_param(monkeypatch):
    backend = HermesGatewayBackend(port=9999)
    calls = _patch_httpx(monkeypatch, [])

    _run(_collect(backend, "sess-1"))

    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:9999/api/sessions/sess-1/chat/stream"
    assert calls[0]["headers"]["X-Hermes-Session-Token"] == backend._token
    assert "token=" not in calls[0]["url"]


def test_stream_tool_events_no_events_yields_nothing(monkeypatch):
    backend = HermesGatewayBackend(port=9999)
    _patch_httpx(monkeypatch, [])

    events = _run(_collect(backend, "sess-1"))

    assert events == []
