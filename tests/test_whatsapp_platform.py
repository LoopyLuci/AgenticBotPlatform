"""bot/platforms/whatsapp_platform.py — the WhatsApp Cloud API adapter's
webhook verification, HMAC signature check, and message handling.
Exercises real bot_instances rows against temp_db, with only the
outbound Graph API HTTP calls faked (no live Meta endpoint to hit).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from bot import bot_instances, db, platform_supervisor
from bot.platforms import whatsapp_platform


class _FakeTask:
    """Stands in for an asyncio.Task in platform_supervisor._Handle —
    only .done() is ever read by is_running()."""

    def done(self):
        return False


def _run(coro):
    return asyncio.run(coro)


def _create_instance(temp_db, **overrides):
    creds = {
        "phone_number_id": "1000000000",
        "access_token": "EAAG_fake_token_long_enough",
        "app_secret": "supersecretappsecret",
        "verify_token": "my-verify-token",
    }
    creds.update(overrides.pop("credentials", {}))
    return bot_instances.create_instance(
        name=overrides.pop("name", "wa-bot"), platform="whatsapp", backend="cli",
        credentials=creds, allowed_user_ids=overrides.pop("allowed_user_ids", ["15551234567"]),
        **overrides,
    )


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(f"http {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)


def _install_fake_client(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr("bot.platforms.whatsapp_platform.httpx.AsyncClient", lambda **kwargs: fake)
    return fake


# ------------------------------------------------------------- verification

def test_verify_challenge_accepts_matching_token(temp_db):
    _create_instance(temp_db)
    assert whatsapp_platform.verify_challenge("subscribe", "my-verify-token", "chal123") == "chal123"


def test_verify_challenge_rejects_wrong_token(temp_db):
    _create_instance(temp_db)
    assert whatsapp_platform.verify_challenge("subscribe", "wrong-token", "chal123") is None


def test_verify_challenge_rejects_wrong_mode(temp_db):
    _create_instance(temp_db)
    assert whatsapp_platform.verify_challenge("unsubscribe", "my-verify-token", "chal123") is None


def test_verify_signature_accepts_real_hmac(temp_db):
    _create_instance(temp_db)
    body = b'{"entry": []}'
    sig = "sha256=" + hmac.new(b"supersecretappsecret", body, hashlib.sha256).hexdigest()
    assert whatsapp_platform.verify_signature(body, sig) is True


def test_verify_signature_rejects_wrong_secret(temp_db):
    _create_instance(temp_db)
    body = b'{"entry": []}'
    sig = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    assert whatsapp_platform.verify_signature(body, sig) is False


def test_verify_signature_rejects_malformed_header(temp_db):
    _create_instance(temp_db)
    assert whatsapp_platform.verify_signature(b"{}", "not-a-real-header") is False


# --------------------------------------------------------------- messaging

def test_find_instance_requires_running_supervisor_task(temp_db):
    instance_id = _create_instance(temp_db)
    assert whatsapp_platform._find_instance("1000000000") is None  # not "running"
    platform_supervisor._handles[instance_id] = platform_supervisor._Handle(
        instance_id=instance_id, name="wa-bot", platform="whatsapp", task=_FakeTask()
    )
    try:
        found = whatsapp_platform._find_instance("1000000000")
        assert found is not None
        assert found["id"] == instance_id
    finally:
        platform_supervisor._handles.pop(instance_id, None)


def test_process_message_rejects_unauthorized_sender(temp_db):
    instance_id = _create_instance(temp_db, allowed_user_ids=["15551234567"])
    instance = bot_instances.get_instance(instance_id)
    msg = {"from": "19998887777", "type": "text", "text": {"body": "let me in"}}
    _run(whatsapp_platform._process_message(instance, msg, "Mallory"))
    audits = db.get_conn().execute("SELECT * FROM audit_log WHERE action='unauthorized_attempt'").fetchall()
    assert len(audits) == 1
    assert "19998887777" in audits[0]["detail"]


def test_process_message_dispatches_command(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db)
    instance = bot_instances.get_instance(instance_id)

    async def fail_ask(*a, **k):
        raise AssertionError("router.ask should not run for a slash command")

    monkeypatch.setattr("bot.platforms.whatsapp_platform.router.ask", fail_ask)
    fake = _install_fake_client(monkeypatch, [_FakeResponse(200)])
    msg = {"from": "15551234567", "type": "text", "text": {"body": "/help"}}
    _run(whatsapp_platform._process_message(instance, msg, "Alice"))
    assert fake.calls, "expected a reply to be sent"


def test_process_message_routes_plain_text_to_router(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db)
    instance = bot_instances.get_instance(instance_id)

    class _Result:
        text = "a real reply"

    async def fake_ask(text, **kwargs):
        return _Result()

    monkeypatch.setattr("bot.platforms.whatsapp_platform.router.ask", fake_ask)
    fake = _install_fake_client(monkeypatch, [_FakeResponse(200)])
    msg = {"from": "15551234567", "type": "text", "text": {"body": "hello there"}}
    _run(whatsapp_platform._process_message(instance, msg, "Alice"))
    sent_json = fake.calls[0][2]["json"]
    assert sent_json["text"]["body"] == "a real reply"


def test_process_message_ignores_non_message_types(temp_db):
    instance_id = _create_instance(temp_db)
    instance = bot_instances.get_instance(instance_id)
    msg = {"from": "15551234567", "type": "reaction", "reaction": {"emoji": "👍"}}
    _run(whatsapp_platform._process_message(instance, msg, "Alice"))
    rows = db.get_conn().execute("SELECT * FROM messages WHERE platform='whatsapp'").fetchall()
    assert rows == []


def test_send_text_splits_long_messages_and_logs(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db)
    instance = bot_instances.get_instance(instance_id)
    fake = _install_fake_client(monkeypatch, [_FakeResponse(200), _FakeResponse(200)])
    long_text = "x" * (whatsapp_platform.WHATSAPP_MAX_LEN + 10)
    _run(whatsapp_platform._send_text(instance, "15551234567", long_text))
    assert len(fake.calls) == 2
    row = db.get_conn().execute("SELECT * FROM messages WHERE platform='whatsapp' AND direction='out'").fetchone()
    assert row["text"] == long_text


def test_handle_webhook_payload_routes_to_matching_instance(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db)
    platform_supervisor._handles[instance_id] = platform_supervisor._Handle(
        instance_id=instance_id, name="wa-bot", platform="whatsapp", task=_FakeTask()
    )

    class _Result:
        text = "handled"

    async def fake_ask(text, **kwargs):
        return _Result()

    monkeypatch.setattr("bot.platforms.whatsapp_platform.router.ask", fake_ask)
    fake = _install_fake_client(monkeypatch, [_FakeResponse(200)])
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "1000000000"},
                    "contacts": [{"wa_id": "15551234567", "profile": {"name": "Alice"}}],
                    "messages": [{"from": "15551234567", "type": "text", "text": {"body": "hi"}}],
                },
            }],
        }],
    }
    try:
        _run(whatsapp_platform.handle_webhook_payload(payload))
    finally:
        platform_supervisor._handles.pop(instance_id, None)
    assert fake.calls
