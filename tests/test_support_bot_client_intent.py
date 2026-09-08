"""SupportBot.handle()'s optional client_intent fast-path (Phase 6 of
the "Support Bot NLU upgrade" plan) — a hint from the Android app's
on-device Kotlin classifier that can only ever save a reclassification
round-trip, never bypass real server-side validation/gating.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.support_bot.engine import SupportBot


def _run(coro):
    return asyncio.run(coro)


def test_valid_client_intent_skips_reclassification(temp_db, monkeypatch):
    bot = SupportBot()
    called = {"classify": False}

    class _FakeResult:
        intent = "status"

    def _fake_classify(text, **kw):
        called["classify"] = True
        return _FakeResult()

    monkeypatch.setattr("bot.support_bot.engine.hybrid.classify", _fake_classify)

    reply = _run(bot.handle("what's going on", actor="test", client_intent="status"))

    assert called["classify"] is False
    assert reply.intent == "status"
    assert reply.applied is True


def test_unknown_client_intent_falls_back_to_real_classification(temp_db, monkeypatch):
    bot = SupportBot()

    class _FakeResult:
        intent = "status"

    monkeypatch.setattr("bot.support_bot.engine.hybrid.classify", lambda text, **kw: _FakeResult())

    reply = _run(bot.handle("what's going on", actor="test", client_intent="not_a_real_intent"))

    assert reply.intent == "status"


def test_missing_client_intent_behaves_exactly_as_before(temp_db, monkeypatch):
    bot = SupportBot()

    class _FakeResult:
        intent = "status"

    monkeypatch.setattr("bot.support_bot.engine.hybrid.classify", lambda text, **kw: _FakeResult())

    reply = _run(bot.handle("what's going on", actor="test"))

    assert reply.intent == "status"


def test_client_intent_for_a_destructive_action_still_requires_confirmation(temp_db, monkeypatch):
    """The core safety guarantee: even if the client asserts a
    destructive intent directly, the SAME confirm-gate runs — a client
    can never skip it just by naming the intent."""
    bot = SupportBot()

    reply = _run(bot.handle("shut it down", actor="test", client_intent="bot_disable"))

    assert reply.needs_confirm is True
    assert reply.applied is False
    assert reply.intent == "bot_disable"
