"""New dashboard routes for manager/worker coordination:
/api/context/* (shared cross-instance markdown docs) and
/api/bots/{id}/profile (an instance's own identity as markdown).
Exercised against the real FastAPI app via TestClient.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_instance(**overrides):
    kwargs = dict(
        name="worker-bot", platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


# ------------------------------------------------------------------ profile


def test_profile_route_returns_markdown(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_instance(custom_instructions="Be terse.")

    resp = client.get(f"/api/bots/{instance_id}/profile", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["instance_id"] == instance_id
    assert "worker-bot" in body["markdown"]
    assert "Be terse." in body["markdown"]


def test_profile_route_404_for_missing_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/bots/999999/profile", headers=_headers())
    assert resp.status_code == 404


# -------------------------------------------------------------------- context


def test_context_round_trips_through_routes(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/context/status", headers=_headers(), json={"content": "all green", "actor": "manager"})
    assert resp.status_code == 200
    assert resp.json()["doc"]["content"] == "all green"

    resp = client.get("/api/context/status", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["content"] == "all green"
    assert resp.json()["updated_by"] == "manager"


def test_context_get_missing_doc_is_404(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/context/nope", headers=_headers())
    assert resp.status_code == 404


def test_context_set_requires_content(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/context/status", headers=_headers(), json={})
    assert resp.status_code == 400


def test_context_list_shows_every_doc(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/context/status", headers=_headers(), json={"content": "a"})
    client.post("/api/context/notes", headers=_headers(), json={"content": "bb"})

    resp = client.get("/api/context", headers=_headers())

    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()["docs"]}
    # A fresh DB always seeds the how-to-use-context example doc too (see
    # bot.shared_context.seed_default_docs) — assert our two are present
    # rather than asserting the exact set, so this doesn't fight the seed.
    assert {"status", "notes"} <= names


def test_context_delete(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/context/status", headers=_headers(), json={"content": "a"})

    resp = client.delete("/api/context/status", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["removed"] is True

    resp = client.get("/api/context/status", headers=_headers())
    assert resp.status_code == 404


def test_context_rejects_invalid_name(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/context/has space", headers=_headers(), json={"content": "a"})
    assert resp.status_code == 400


def test_context_rejects_oversized_content(temp_db, monkeypatch):
    from bot import shared_context

    client = _client(monkeypatch)
    resp = client.post(
        "/api/context/status", headers=_headers(),
        json={"content": "x" * (shared_context.MAX_DOC_CHARS + 1)},
    )
    assert resp.status_code == 400
