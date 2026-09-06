"""DELETE /api/chat/messages/{id} — deleting one message from a bot's
local chat history (distinct from the existing bulk
DELETE /api/chat/messages, which clears a whole chat/instance).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def test_delete_one_message(temp_db, monkeypatch):
    client = _client(monkeypatch)
    msg_id = db.log_message(chat_id="12345", direction="in", source="telegram", text="hello")

    resp = client.delete(f"/api/chat/messages/{msg_id}", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.get_message(msg_id) is None


def test_delete_nonexistent_message_is_404(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.delete("/api/chat/messages/999999", headers=_headers())
    assert resp.status_code == 404


def test_delete_requires_auth(temp_db, monkeypatch):
    client = _client(monkeypatch)
    msg_id = db.log_message(chat_id="12345", direction="in", source="telegram", text="hello")
    resp = client.delete(f"/api/chat/messages/{msg_id}")
    assert resp.status_code == 401
