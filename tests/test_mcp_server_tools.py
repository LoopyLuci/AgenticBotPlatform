"""bot/mcp_server.py's tool functions — thin proxies to the dashboard API
via _request(). Most of this file's correctness is already covered by
testing the dashboard routes it proxies (no other test file imports
bot.mcp_server directly); this one is an exception because
write_project_context's actor attribution was a real, silently-wrong
default (see the "Fix actor attribution" commit) worth locking in
directly against the tool's own call shape, not just the route it hits.
"""

from __future__ import annotations

import asyncio

from bot import mcp_server


def _run(coro):
    return asyncio.run(coro)


def _fake_request(monkeypatch, captured):
    async def fake(method, path, timeout=15.0, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_request", fake)


def test_write_project_context_defaults_actor_to_claude(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.write_project_context(name="status", content="hi"))

    assert captured["kwargs"]["json"] == {"content": "hi", "actor": "claude"}


def test_write_project_context_honors_explicit_actor(monkeypatch):
    # Found via live use: a Hermes agent calling this through
    # enable_hermes_swarm_tools had every write attributed to the generic
    # "claude" default regardless of which real agent wrote it, making
    # the doc's "who last touched this" metadata meaningless for
    # distinguishing multiple callers of this one MCP server.
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.write_project_context(name="status", content="hi", actor="Hermes Telegram"))

    assert captured["kwargs"]["json"] == {"content": "hi", "actor": "Hermes Telegram"}
    assert captured["path"] == "/api/context/status"
    assert captured["method"] == "POST"
