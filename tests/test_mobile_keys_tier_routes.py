"""Per-device permission-tier enforcement on /api/mobile-keys* — minting is
capped at the minting device's own tier, retiering/revoking another
device requires it be strictly lower-ranked, and the desktop
DASHBOARD_TOKEN is always the unconditional top authority. See
bot/device_tiers.py and the "Admin control surface" plan.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("DASHBOARD_PORT", "8787")
    return TestClient(build_app())


def _desktop_headers():
    return {"X-Dashboard-Token": "test-token"}


def _device_headers(plaintext):
    return {"X-Dashboard-Token": plaintext}


def test_desktop_can_mint_at_any_tier(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/mobile-keys", headers=_desktop_headers(), json={"label": "Phone", "tier": "unrestricted"})
    assert resp.status_code == 200


def test_device_cannot_mint_above_its_own_tier(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, standard_key = db.create_api_key("Standard Phone", permission_tier="standard")

    resp = client.post("/api/mobile-keys", headers=_device_headers(standard_key), json={"label": "New Tablet", "tier": "elevated"})

    assert resp.status_code == 403


def test_device_can_mint_a_peer_at_its_own_tier(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, elevated_key = db.create_api_key("Elevated Phone", permission_tier="elevated")

    resp = client.post("/api/mobile-keys", headers=_device_headers(elevated_key), json={"label": "New Tablet", "tier": "elevated"})

    assert resp.status_code == 200


def test_unknown_tier_rejected(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/mobile-keys", headers=_desktop_headers(), json={"label": "Phone", "tier": "superadmin"})
    assert resp.status_code == 400


def test_desktop_can_retier_any_device(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, _ = db.create_api_key("Phone", permission_tier="none")

    resp = client.post(f"/api/mobile-keys/{key_id}/tier", headers=_desktop_headers(), json={"tier": "unrestricted"})

    assert resp.status_code == 200
    assert db.get_api_key(key_id)["permission_tier"] == "unrestricted"


def test_device_can_retier_a_strictly_lower_device(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, high_key = db.create_api_key("High Phone", permission_tier="unrestricted")
    low_id, _ = db.create_api_key("Low Phone", permission_tier="none")

    resp = client.post(f"/api/mobile-keys/{low_id}/tier", headers=_device_headers(high_key), json={"tier": "standard"})

    assert resp.status_code == 200
    assert db.get_api_key(low_id)["permission_tier"] == "standard"


def test_device_cannot_retier_a_peer(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, a_key = db.create_api_key("Phone A", permission_tier="elevated")
    b_id, _ = db.create_api_key("Phone B", permission_tier="elevated")

    resp = client.post(f"/api/mobile-keys/{b_id}/tier", headers=_device_headers(a_key), json={"tier": "none"})

    assert resp.status_code == 403


def test_device_cannot_retier_itself(temp_db, monkeypatch):
    client = _client(monkeypatch)
    key_id, key = db.create_api_key("Phone", permission_tier="unrestricted")

    resp = client.post(f"/api/mobile-keys/{key_id}/tier", headers=_device_headers(key), json={"tier": "none"})

    assert resp.status_code == 403


def test_device_cannot_grant_a_tier_above_its_own(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, mid_key = db.create_api_key("Mid Phone", permission_tier="standard")
    low_id, _ = db.create_api_key("Low Phone", permission_tier="none")

    resp = client.post(f"/api/mobile-keys/{low_id}/tier", headers=_device_headers(mid_key), json={"tier": "unrestricted"})

    assert resp.status_code == 403


def test_device_can_revoke_a_strictly_lower_device(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _, high_key = db.create_api_key("High Phone", permission_tier="unrestricted")
    low_id, _ = db.create_api_key("Low Phone", permission_tier="none")

    resp = client.post(f"/api/mobile-keys/{low_id}/revoke-by-device", headers=_device_headers(high_key))

    assert resp.status_code == 200
    assert db.get_api_key(low_id)["revoked_at"] is not None


def test_device_cannot_revoke_a_peer_or_itself(temp_db, monkeypatch):
    client = _client(monkeypatch)
    a_id, a_key = db.create_api_key("Phone A", permission_tier="elevated")
    b_id, _ = db.create_api_key("Phone B", permission_tier="elevated")

    assert client.post(f"/api/mobile-keys/{b_id}/revoke-by-device", headers=_device_headers(a_key)).status_code == 403
    assert client.post(f"/api/mobile-keys/{a_id}/revoke-by-device", headers=_device_headers(a_key)).status_code == 403
