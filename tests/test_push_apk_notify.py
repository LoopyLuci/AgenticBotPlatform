"""notify_apk_push — the targeted FCM wake-up sent when an operator
queues an APK push for a device (dashboard "Send APK"/"Send to all", or
a phone's own mesh send). Before this, a device only ever found out
about a pending push on its own next GET /api/android/apk/pending poll
(i.e. whenever the user happened to reopen the Devices screen) — this
is the fix for "pushing an update doesn't actually reach the device."

Faked at the same httpx boundary test_hermes_model_discovery.py already
established. _access_token is monkeypatched directly rather than faking
the OAuth2 JWT exchange too — that flow is already exercised by
whatever covers notify_new_message's token caching, not the point here.
"""

from __future__ import annotations

import asyncio

from bot import db, push


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response


def _fake_service_account():
    return {"client_email": "x@example.com", "private_key": "irrelevant", "project_id": "proj-123"}


def test_no_op_when_fcm_not_configured(monkeypatch, temp_db):
    monkeypatch.setattr(push, "_service_account", lambda: None)
    # Should not raise, and obviously shouldn't touch the network.
    _run(push.notify_apk_push(1, 42, "v1.0.0"))


def test_notifies_only_the_targeted_devices_token(monkeypatch, temp_db):
    key_a_id, _ = db.create_api_key("phone-a")
    key_b_id, _ = db.create_api_key("phone-b")
    db.upsert_push_token(key_a_id, "token-a")
    db.upsert_push_token(key_b_id, "token-b")

    fake_client = _FakeAsyncClient(_FakeResponse(200))
    monkeypatch.setattr(push, "_service_account", _fake_service_account)

    async def _fake_access_token(account):
        return "fake-access-token"

    monkeypatch.setattr(push, "_access_token", _fake_access_token)
    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=None: fake_client)

    _run(push.notify_apk_push(key_a_id, 42, "v1.0.0"))

    assert len(fake_client.calls) == 1
    sent = fake_client.calls[0]["json"]["message"]
    assert sent["token"] == "token-a"
    # Data-only — no "notification" key, so this always reaches
    # onMessageReceived() even while the app is backgrounded.
    assert "notification" not in sent
    assert sent["data"] == {"type": "apk_update", "push_id": "42", "version_label": "v1.0.0"}


def test_stale_token_is_dropped_on_unregistered_response(monkeypatch, temp_db):
    key_id, _ = db.create_api_key("phone-a")
    db.upsert_push_token(key_id, "stale-token")

    fake_client = _FakeAsyncClient(_FakeResponse(404, "not found"))
    monkeypatch.setattr(push, "_service_account", _fake_service_account)

    async def _fake_access_token(account):
        return "fake-access-token"

    monkeypatch.setattr(push, "_access_token", _fake_access_token)
    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=None: fake_client)

    _run(push.notify_apk_push(key_id, 1, None))

    remaining = [row["fcm_token"] for row in db.list_push_tokens()]
    assert "stale-token" not in remaining
