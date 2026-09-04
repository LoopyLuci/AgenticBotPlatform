"""GET /api/network-info and POST /api/mobile-keys' auto-fill behavior —
a freshly-minted pairing key should get two independent, automatically
detected network paths (LAN + Tailscale) by default when the operator
doesn't type one explicitly, per bot/network_info.py's module doc. The
underlying detection itself has its own dedicated coverage
(test_network_info.py); this only needs to prove the route wires it in
correctly and respects explicit operator input.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("DASHBOARD_PORT", "8787")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _fake_addresses(monkeypatch, lan=None, tailscale=None):
    from bot import network_info

    monkeypatch.setattr(network_info, "detect_addresses", lambda: {"lan": lan, "tailscale": tailscale})


def test_network_info_route_reports_detected_addresses(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77")
    client = _client(monkeypatch)

    resp = client.get("/api/network-info", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == {"lan": "192.168.1.50:8787", "tailscale": "100.101.98.77:8787"}


def test_network_info_route_nulls_when_nothing_detected(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan=None, tailscale=None)
    client = _client(monkeypatch)

    resp = client.get("/api/network-info", headers=_headers())

    assert resp.json() == {"lan": None, "tailscale": None}


def test_network_info_route_reachable_by_a_paired_mobile_device_key(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale=None)
    client = _client(monkeypatch)
    _, plaintext = db.create_api_key("test-phone", kind="device")

    resp = client.get("/api/network-info", headers={"X-Dashboard-Token": plaintext})

    assert resp.status_code == 200


def test_create_mobile_key_auto_fills_both_hosts_when_none_given(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77")
    client = _client(monkeypatch)

    resp = client.post("/api/mobile-keys", headers=_headers(), json={"label": "My Phone"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "192.168.1.50:8787"
    assert body["host2"] == "100.101.98.77:8787"


def test_create_mobile_key_only_fills_the_blank_host(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77")
    client = _client(monkeypatch)

    resp = client.post(
        "/api/mobile-keys", headers=_headers(),
        json={"label": "My Phone", "host": "manual-host.example:8787"},
    )

    assert resp.status_code == 200
    body = resp.json()
    # The operator's explicit host is left untouched; only the blank host2
    # slot gets auto-filled, and with the LAN address (first in candidate
    # order) since it doesn't collide with the manual host.
    assert body["host"] == "manual-host.example:8787"
    assert body["host2"] == "192.168.1.50:8787"


def test_create_mobile_key_leaves_hosts_blank_when_nothing_detected(temp_db, monkeypatch):
    _fake_addresses(monkeypatch, lan=None, tailscale=None)
    client = _client(monkeypatch)

    resp = client.post("/api/mobile-keys", headers=_headers(), json={"label": "My Phone"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] is None
    assert body["host2"] is None
