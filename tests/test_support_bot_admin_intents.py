"""Support Bot's new admin-shaped intents (estop_status/engage/disengage,
Section 4 of the "Admin control surface" plan) — gated by the calling
device's own permission_tier rather than an is_admin_instance flag,
since Support Bot has no bot_instances concept of its own.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from bot.agent_runtime import estop
from bot.dashboard.server import build_app
from bot.support_bot.engine import SupportBot


def _run(coro):
    return asyncio.run(coro)


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def test_default_device_tier_is_unrestricted_and_unaffected(temp_db):
    """Every existing caller of handle() that never passes device_tier
    keeps working exactly as before this parameter existed."""
    bot = SupportBot()
    reply = _run(bot.handle("is the emergency stop engaged", actor="test"))
    assert reply.intent == "estop_status"
    assert "not engaged" in reply.text.lower()


def test_estop_status_requires_at_least_standard_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("is the emergency stop engaged", actor="test", device_tier="none"))
    assert "permission tier" in reply.text.lower()


def test_estop_status_readable_at_standard_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("is the emergency stop engaged", actor="test", device_tier="standard"))
    assert "not engaged" in reply.text.lower()


def test_estop_engage_refused_below_elevated_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("engage the emergency stop", actor="test", device_tier="standard"))
    assert "permission tier" in reply.text.lower()
    assert not estop.is_engaged()


def test_estop_engage_requires_confirmation_even_at_elevated_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("engage the emergency stop", actor="test", device_tier="elevated"))
    assert reply.needs_confirm is True
    assert not estop.is_engaged()


def test_estop_engage_and_disengage_full_roundtrip_at_unrestricted_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("engage the emergency stop", actor="test", device_tier="unrestricted"))
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert estop.is_engaged()
    assert confirmed.applied is True

    reply2 = _run(bot.handle("disengage the emergency stop", actor="test", device_tier="unrestricted"))
    _run(bot.confirm(reply2.confirm_token, actor="test"))
    assert not estop.is_engaged()


def test_ask_route_resolves_device_tier_from_the_caller(temp_db, monkeypatch):
    from bot import db

    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone", permission_tier="none")

    resp = client.post(
        "/api/support-bot/ask", headers={"X-Dashboard-Token": plaintext}, json={"text": "engage the emergency stop"},
    )

    assert resp.status_code == 200
    assert "permission tier" in resp.json()["text"].lower()


def test_ask_route_from_desktop_token_is_unrestricted(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/support-bot/ask", headers=_auth(), json={"text": "is the emergency stop engaged"})

    assert resp.status_code == 200
    assert "not engaged" in resp.json()["text"].lower()
