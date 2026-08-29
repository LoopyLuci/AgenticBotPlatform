"""bot/dashboard/server.py's /webhooks/whatsapp routes — the one pair of
endpoints in this app that are deliberately unauthenticated (Meta can't
send a dashboard token), so their actual security boundary (the
X-Hub-Signature-256 HMAC check) needs to be exercised end to end through
the real FastAPI app, not just the pure function it delegates to.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from bot import bot_instances
from bot.dashboard.server import build_app


def _create_instance(**overrides):
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


def test_get_verification_succeeds_with_matching_token(temp_db):
    _create_instance()
    client = TestClient(build_app())
    resp = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "my-verify-token", "hub.challenge": "abc123"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc123"


def test_get_verification_rejects_wrong_token(temp_db):
    _create_instance()
    client = TestClient(build_app())
    resp = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "abc123"},
    )
    assert resp.status_code == 403


def test_post_webhook_rejects_missing_signature(temp_db):
    _create_instance()
    client = TestClient(build_app())
    resp = client.post("/webhooks/whatsapp", content=b'{"entry": []}')
    assert resp.status_code == 403


def test_post_webhook_rejects_bad_signature(temp_db):
    _create_instance()
    client = TestClient(build_app())
    resp = client.post(
        "/webhooks/whatsapp", content=b'{"entry": []}',
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 403


def test_post_webhook_accepts_valid_signature(temp_db):
    _create_instance()
    client = TestClient(build_app())
    body = json.dumps({"entry": []}).encode()
    sig = "sha256=" + hmac.new(b"supersecretappsecret", body, hashlib.sha256).hexdigest()
    resp = client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
