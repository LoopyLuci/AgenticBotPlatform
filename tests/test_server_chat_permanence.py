"""The permanent Server Chat group room can't be cleared or have its
messages individually deleted — closes the gap where only full row
deletion was previously refused. Direct (1:1) conversations remain
fully clearable/deletable exactly as before. See the "Admin control
surface" plan.
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


def test_clearing_the_group_room_is_refused(temp_db, monkeypatch):
    client = _client(monkeypatch)
    group_id = db.ensure_server_chat_group()
    db.create_server_chat_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "hello everyone")

    resp = client.delete(f"/api/server-chat/conversations/{group_id}", headers=_desktop_headers())

    assert resp.status_code == 400
    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM server_chat_messages WHERE conversation_id=?", (group_id,)).fetchone() is not None


def test_deleting_a_group_room_message_is_refused(temp_db, monkeypatch):
    client = _client(monkeypatch)
    group_id = db.ensure_server_chat_group()
    msg_id = db.create_server_chat_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "hello everyone")

    resp = client.delete(f"/api/server-chat/messages/{msg_id}", headers=_desktop_headers())

    assert resp.status_code == 400
    assert db.get_server_chat_message(msg_id) is not None


def test_direct_conversations_remain_fully_clearable(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    db.create_server_chat_message(conversation_id, key_id, "hi")

    resp = client.delete(f"/api/server-chat/conversations/{conversation_id}", headers=_device_headers(plaintext))

    assert resp.status_code == 200


def test_direct_conversation_messages_remain_deletable(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, plaintext = db.create_api_key("phone-a")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)
    msg_id = db.create_server_chat_message(conversation_id, key_id, "hi")

    resp = client.delete(f"/api/server-chat/messages/{msg_id}", headers=_device_headers(plaintext))

    assert resp.status_code == 200
    assert db.get_server_chat_message(msg_id) is None
