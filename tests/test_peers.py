"""A linked peer server (bot/peers.py) authenticates with an api_keys row of
kind "peer_server" - the SAME table a paired phone's key lives in. Before
this test file existed, nothing distinguished the two: a peer's key got
full desktop-equivalent access to every _require_token_or_api_key route,
including unredacted bot credentials (GET /api/bots), the live /api/ws
broadcast firehose, and mesh/APK-push device routes. This covers the fix:
a peer's key may only reach the narrow overview/bots(redacted)/lifecycle
surface bot/peers.py's own proxy actually calls, and gets 401/403 (or
never sees plaintext credentials) everywhere else.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances, db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _peer_headers():
    _, plaintext = db.create_api_key("peer: other-machine", kind="peer_server")
    return {"X-Dashboard-Token": plaintext}


def _mobile_headers():
    _, plaintext = db.create_api_key("test-phone", kind="device")
    return {"X-Dashboard-Token": plaintext}


_TOKEN = "123456789:AAExampleTokenFromBotFather1234"


def test_peer_key_sees_bots_list_but_credentials_are_redacted(temp_db, monkeypatch):
    client = _client(monkeypatch)
    bot_instances.create_instance(
        name="real bot", platform="telegram", backend="ui", credentials={"bot_token": _TOKEN},
        allowed_user_ids=[1],
    )

    resp = client.get("/api/bots", headers=_peer_headers())
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["credentials"] == {"bot_token": "***"}
    assert _TOKEN not in resp.text


def test_mobile_key_still_sees_real_credentials_on_bots_list(temp_db, monkeypatch):
    """The redaction is peer-specific — a paired phone keeps today's full,
    unredacted access (the desktop app needs it to let you edit a token)."""
    client = _client(monkeypatch)
    bot_instances.create_instance(
        name="real bot", platform="telegram", backend="ui", credentials={"bot_token": _TOKEN},
        allowed_user_ids=[1],
    )

    resp = client.get("/api/bots", headers=_mobile_headers())
    assert resp.status_code == 200
    assert resp.json()[0]["credentials"] == {"bot_token": _TOKEN}


def test_peer_key_can_reach_overview_and_lifecycle_actions(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = bot_instances.create_instance(
        name="real bot", platform="telegram", backend="ui", credentials={"bot_token": _TOKEN}, allowed_user_ids=[1],
    )
    headers = _peer_headers()

    assert client.get("/api/overview", headers=headers).status_code == 200
    assert client.post(f"/api/bots/{instance_id}/enable", headers=headers).status_code == 200
    assert client.post(f"/api/bots/{instance_id}/disable", headers=headers).status_code == 200


def test_peer_key_is_rejected_on_routes_it_has_no_business_calling(temp_db, monkeypatch):
    client = _client(monkeypatch)
    headers = _peer_headers()

    assert client.post("/api/config/set", headers=headers, json={"path": "x", "value": "y"}).status_code == 403
    assert client.get("/api/devices", headers=headers).status_code == 403
    assert client.post(
        "/api/chat/send-to-bot", headers=headers, json={"instance_id": 1, "text": "hi"}
    ).status_code == 403
    assert client.get("/api/bots/1", headers=headers).status_code == 403


def test_peer_key_cannot_open_the_live_broadcast_websocket(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, plaintext = db.create_api_key("peer: other-machine", kind="peer_server")

    try:
        with client.websocket_connect(f"/api/ws?token={plaintext}"):
            raise AssertionError("peer key must not be able to open /api/ws")
    except Exception:
        pass  # starlette's TestClient raises on the server-side close(4401)


def test_mobile_key_can_still_open_the_broadcast_websocket(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, plaintext = db.create_api_key("test-phone", kind="device")

    with client.websocket_connect(f"/api/ws?token={plaintext}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "device_list"


def test_peer_key_cannot_use_mesh_or_push_routes(temp_db, monkeypatch):
    client = _client(monkeypatch)
    headers = _peer_headers()

    assert client.post("/api/push/register", headers=headers, json={"fcm_token": "x"}).status_code == 401
    assert client.post(
        "/api/android/apk/send", headers=headers, json={"api_key_id": 1}
    ).status_code == 401
