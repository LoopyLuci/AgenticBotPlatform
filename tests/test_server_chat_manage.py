"""Server Chat management: rename, bulk clear/delete, search, desktop moderation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app

D = db.SERVER_CHAT_DESKTOP_DEVICE_ID
H = {"X-Dashboard-Token": "test-token"}


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_rename_direct_and_reset(temp_db, monkeypatch):
    c = _client(monkeypatch)
    key, _ = db.create_api_key("phone-a")
    cid = db.ensure_direct_conversation(D, key)
    assert c.patch(f"/api/server-chat/conversations/{cid}", json={"title": "Mum"}, headers=H).status_code == 200
    assert [x["title"] for x in db.list_server_chat_conversations(D) if x["id"] == cid] == ["Mum"]
    c.patch(f"/api/server-chat/conversations/{cid}", json={"title": ""}, headers=H)
    assert [x["title"] for x in db.list_server_chat_conversations(D) if x["id"] == cid] != ["Mum"]


def test_rename_group_refused(temp_db, monkeypatch):
    c = _client(monkeypatch)
    g = db.ensure_server_chat_group()
    assert c.patch(f"/api/server-chat/conversations/{g}", json={"title": "x"}, headers=H).status_code == 400


def test_bulk_delete_full_skips_group(temp_db, monkeypatch):
    c = _client(monkeypatch)
    g = db.ensure_server_chat_group()
    k1, _ = db.create_api_key("a")
    k2, _ = db.create_api_key("b")
    c1, c2 = db.ensure_direct_conversation(D, k1), db.ensure_direct_conversation(D, k2)
    db.create_server_chat_message(c1, D, "x")
    r = c.post("/api/server-chat/conversations/bulk-delete", json={"ids": [c1, g], "full": True}, headers=H).json()
    assert r["done"] == [c1] and r["skipped"] == [g]
    ids = [x["id"] for x in db.list_server_chat_conversations(D)]
    assert c1 not in ids and c2 in ids and g in ids
    assert db.list_server_chat_messages(c1) == []


def test_bulk_clear_keeps_conversation(temp_db, monkeypatch):
    c = _client(monkeypatch)
    k, _ = db.create_api_key("a")
    cid = db.ensure_direct_conversation(D, k)
    db.create_server_chat_message(cid, D, "x")
    c.post("/api/server-chat/conversations/bulk-delete", json={"ids": [cid]}, headers=H)
    assert cid in [x["id"] for x in db.list_server_chat_conversations(D)]
    assert db.list_server_chat_messages(cid) == []


def test_search_and_desktop_moderation(temp_db, monkeypatch):
    c = _client(monkeypatch)
    k, _ = db.create_api_key("a")
    cid = db.ensure_direct_conversation(D, k)
    mid = db.create_server_chat_message(cid, k, "needle_50% here")
    assert [r["id"] for r in c.get("/api/server-chat/search", params={"q": "needle_50%"}, headers=H).json()] == [mid]
    assert c.get("/api/server-chat/search", params={"q": "zzz"}, headers=H).json() == []
    assert c.delete(f"/api/server-chat/messages/{mid}", headers=H).status_code == 200
    assert db.get_server_chat_message(mid) is None


def test_full_delete_survives_backfill_until_explicit_reopen(temp_db, monkeypatch):
    c = _client(monkeypatch)
    k, _ = db.create_api_key("a")
    cid = db.ensure_direct_conversation(D, k)
    c.delete(f"/api/server-chat/conversations/{cid}?full=true", headers=H)
    db.backfill_server_chat_conversations()
    assert [x for x in db.list_server_chat_conversations(D) if x["kind"] == "direct"] == []
    new = c.post("/api/server-chat/conversations", json={"peer_device_id": k}, headers=H).json()["conversation_id"]
    db.backfill_server_chat_conversations()
    assert new in [x["id"] for x in db.list_server_chat_conversations(D)]


def test_archive_roundtrip_and_group_refused(temp_db, monkeypatch):
    c = _client(monkeypatch)
    g = db.ensure_server_chat_group()
    k, _ = db.create_api_key("a")
    cid = db.ensure_direct_conversation(D, k)
    assert c.post(f"/api/server-chat/conversations/{cid}/archive", json={}, headers=H).status_code == 200
    assert [x["archived"] for x in db.list_server_chat_conversations(D) if x["id"] == cid] == [1]
    c.post(f"/api/server-chat/conversations/{cid}/archive", json={"archived": False}, headers=H)
    assert [x["archived"] for x in db.list_server_chat_conversations(D) if x["id"] == cid] == [0]
    assert c.post(f"/api/server-chat/conversations/{g}/archive", json={}, headers=H).status_code == 400
