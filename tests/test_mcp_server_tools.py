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


# ------------------------------------------- support bot NLU MCP tools ----
# Next-generation modular hybrid plan, Phase 9. Most of these are thin
# proxies (covered by their own dashboard route tests); the two with
# real logic in the tool function itself — module-id filtering and
# decision validation — get direct coverage here, same reasoning as the
# rest of this file.

def test_support_bot_generate_training_data_defaults_target(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_generate_training_data(module_id="bots"))

    assert captured["path"] == "/api/support-bot/generate/run"
    assert captured["kwargs"]["json"] == {"module_id": "bots", "target_per_intent": 20}


def test_support_bot_list_pending_examples_filters_by_module(monkeypatch):
    async def fake(method, path, timeout=15.0, **kwargs):
        return [
            {"id": 1, "intent": "bot_create", "phrase": "a"},
            {"id": 2, "intent": "mcp_list", "phrase": "b"},
        ]

    monkeypatch.setattr(mcp_server, "_request", fake)

    result = _run(mcp_server.support_bot_list_pending_examples(module_id="bots"))

    assert result == {"examples": [{"id": 1, "intent": "bot_create", "phrase": "a"}]}


def test_support_bot_list_pending_examples_without_module_id_returns_everything(monkeypatch):
    async def fake(method, path, timeout=15.0, **kwargs):
        return [{"id": 1, "intent": "bot_create", "phrase": "a"}, {"id": 2, "intent": "mcp_list", "phrase": "b"}]

    monkeypatch.setattr(mcp_server, "_request", fake)

    result = _run(mcp_server.support_bot_list_pending_examples())

    assert len(result["examples"]) == 2


def test_support_bot_review_pending_example_rejects_an_invalid_decision(monkeypatch):
    result = _run(mcp_server.support_bot_review_pending_example(1, "delete"))
    assert "error" in result


def test_support_bot_review_pending_example_proxies_approve(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_review_pending_example(5, "approve"))

    assert captured["path"] == "/api/support-bot/pending/5/approve"
    assert captured["method"] == "POST"


def test_support_bot_review_pending_example_proxies_revert(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_review_pending_example(5, "revert"))

    assert captured["path"] == "/api/support-bot/pending/5/revert"


def test_support_bot_list_knowledge_modules_proxies_manifest(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_list_knowledge_modules())

    assert captured["path"] == "/api/support-bot/manifest"
    assert captured["method"] == "GET"


def test_support_bot_set_module_enabled_proxies_correctly(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_set_module_enabled("bots", False))

    assert captured["path"] == "/api/support-bot/modules/bots/enabled"
    assert captured["kwargs"]["json"] == {"enabled": False}


def test_support_bot_retrain_module_proxies_correctly(monkeypatch):
    captured = {}
    _fake_request(monkeypatch, captured)

    _run(mcp_server.support_bot_retrain_module("bots", accept_if_regression_under=0.02))

    assert captured["path"] == "/api/support-bot/modules/bots/retrain"
    assert captured["kwargs"]["json"] == {"accept_if_regression_under": 0.02}
