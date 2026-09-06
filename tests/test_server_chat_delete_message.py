"""DELETE /api/server-chat/messages/{id} — deleting a single Server Chat
message, restricted to the device that actually sent it. Exercised
against the real FastAPI app via TestClient, matching
test_file_share_routes.py's precedent.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _desktop_headers():
    return {"X-Dashboard-Token": "test-token"}


def _device_headers(plaintext_key):
    return {"X-Dashboard-Token": plaintext_key}


def test_sender_can_delete_their_own_message(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    msg_id = db.create_server_chat_message(conversation_id, key_id, "hello from the phone")

    resp = client.delete(f"/api/server-chat/messages/{msg_id}", headers=_device_headers(plaintext))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.get_server_chat_message(msg_id) is None


def test_a_different_device_cannot_delete_someone_elses_message(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_a, plaintext_a = db.create_api_key("phone-a")
    key_b, plaintext_b = db.create_api_key("phone-b")
    conversation_id = db.ensure_direct_conversation(key_a, key_b)
    msg_id = db.create_server_chat_message(conversation_id, key_a, "only phone-a should be able to delete this")

    resp = client.delete(f"/api/server-chat/messages/{msg_id}", headers=_device_headers(plaintext_b))
    assert resp.status_code == 403
    # Still there — the forbidden attempt must not have deleted it anyway.
    assert db.get_server_chat_message(msg_id) is not None


def test_deleting_a_nonexistent_message_is_404(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.delete("/api/server-chat/messages/999999", headers=_desktop_headers())
    assert resp.status_code == 404


def test_desktop_can_delete_its_own_message(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, _ = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    msg_id = db.create_server_chat_message(conversation_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "hi from desktop")

    resp = client.delete(f"/api/server-chat/messages/{msg_id}", headers=_desktop_headers())
    assert resp.status_code == 200
    assert db.get_server_chat_message(msg_id) is None
