"""Full conversation deletion (DELETE /api/server-chat/conversations/{id}?full=true)
and reopening one afterward (POST /api/server-chat/conversations) — the
"fully and completely delete entire chats" feature, distinct from the
existing clear-messages-only behavior (full=false, the default).
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


def test_full_delete_removes_the_conversation_row_and_messages(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    db.create_server_chat_message(conversation_id, key_id, "hi")

    resp = client.delete(
        f"/api/server-chat/conversations/{conversation_id}?full=true", headers=_device_headers(plaintext)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted_conversation": True}

    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM server_chat_conversations WHERE id=?", (conversation_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM server_chat_messages WHERE conversation_id=?", (conversation_id,)).fetchone() is None


def test_full_delete_refused_on_the_group_room(temp_db, monkeypatch):
    client = _client(monkeypatch)
    group_id = db.ensure_server_chat_group()

    resp = client.delete(f"/api/server-chat/conversations/{group_id}?full=true", headers=_desktop_headers())
    assert resp.status_code == 400

    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM server_chat_conversations WHERE id=?", (group_id,)).fetchone() is not None


def test_plain_delete_still_only_clears_messages_by_default(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    db.create_server_chat_message(conversation_id, key_id, "hi")

    resp = client.delete(f"/api/server-chat/conversations/{conversation_id}", headers=_device_headers(plaintext))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 1}

    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM server_chat_conversations WHERE id=?", (conversation_id,)).fetchone() is not None


def test_reopening_a_fully_deleted_conversation_recreates_it(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    original_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    db.delete_server_chat_conversation(original_id)

    resp = client.post(
        "/api/server-chat/conversations",
        headers=_device_headers(plaintext),
        json={"peer_device_id": db.SERVER_CHAT_DESKTOP_DEVICE_ID},
    )
    assert resp.status_code == 200
    new_id = resp.json()["conversation_id"]
    assert db.is_conversation_participant(new_id, key_id)
