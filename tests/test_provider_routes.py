"""GET /api/providers/catalog, GET /api/providers/{name}/models, and
POST /api/providers/{name}/models/toggle — the routes backing the
Providers + Models page feature. Exercised against the real FastAPI app
via TestClient (matching test_hermes_swarm_routes.py's precedent), with
the underlying bot.model_pricing/bot.models functions faked so these
stay fast/offline.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db, providers
from bot.config import ConfigManager
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _isolate_provider_registry(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=path))


def test_providers_catalog_route(temp_db, monkeypatch):
    client = _client(monkeypatch)

    async def fake_list_known_providers(refresh=False):
        return [{"id": "openrouter", "name": "OpenRouter", "api": "https://openrouter.ai/api/v1", "env": ["OPENROUTER_API_KEY"]}]

    from bot import model_pricing

    monkeypatch.setattr(model_pricing, "list_known_providers", fake_list_known_providers)

    resp = client.get("/api/providers/catalog", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["providers"][0]["id"] == "openrouter"


def test_provider_models_route_404_for_unconfigured_provider(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.get("/api/providers/nope/models", headers=_headers())

    assert resp.status_code == 404


def test_provider_models_route_returns_browse_result(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    providers.set_provider("openrouter", "https://openrouter.ai/api/v1")

    from bot import models as models_module

    async def fake_browse(name):
        return [{"id": "free-model:free", "free": True, "input": None, "output": None, "enabled": True}]

    monkeypatch.setattr(models_module, "browse_provider_models", fake_browse)

    resp = client.get("/api/providers/openrouter/models", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["models"][0]["id"] == "free-model:free"


def test_toggle_route_requires_model_id(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    providers.set_provider("openrouter", "https://openrouter.ai/api/v1")

    resp = client.post("/api/providers/openrouter/models/toggle", headers=_headers(), json={})

    assert resp.status_code == 400


def test_toggle_route_404_for_unconfigured_provider(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.post(
        "/api/providers/nope/models/toggle", headers=_headers(), json={"model_id": "x", "enabled": False},
    )

    assert resp.status_code == 404


def test_toggle_route_persists_and_logs_audit(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    providers.set_provider("openrouter", "https://openrouter.ai/api/v1")

    # A model id containing "/" — must round-trip correctly since it's
    # sent in the JSON body, never a URL path segment.
    resp = client.post(
        "/api/providers/openrouter/models/toggle", headers=_headers(),
        json={"model_id": "nvidia/nemotron-3-super-120b-a12b:free", "enabled": False},
    )

    assert resp.status_code == 200
    assert db.disabled_model_ids("openrouter") == {"nvidia/nemotron-3-super-120b-a12b:free"}
    audit = db.list_audit_log(actions=["model_toggle"])
    assert len(audit) == 1
    assert "disabled" in audit[0]["detail"]


def test_toggle_paid_route_disables_only_paid_models(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    providers.set_provider("openrouter", "https://openrouter.ai/api/v1")

    from bot import models as models_module

    async def fake_browse(name):
        return [
            {"id": "free-model:free", "free": True, "input": None, "output": None, "enabled": True},
            {"id": "paid-model-a", "free": False, "input": 1e-6, "output": 2e-6, "enabled": False},
            {"id": "paid-model-b", "free": None, "input": None, "output": None, "enabled": False},
        ]

    monkeypatch.setattr(models_module, "browse_provider_models", fake_browse)

    resp = client.post(
        "/api/providers/openrouter/models/toggle-paid", headers=_headers(), json={"enabled": True},
    )

    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    overrides = {r["model_id"]: bool(r["enabled"]) for r in db.list_model_toggles("openrouter")}
    assert overrides == {"paid-model-a": True, "paid-model-b": True}
    assert "free-model:free" not in overrides
    audit = db.list_audit_log(actions=["model_toggle_paid_bulk"])
    assert len(audit) == 1
    assert "2 paid model(s) enabled" in audit[0]["detail"]


def test_toggle_paid_route_404_for_unconfigured_provider(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.post("/api/providers/nope/models/toggle-paid", headers=_headers(), json={"enabled": False})

    assert resp.status_code == 404


def test_providers_routes_reachable_by_a_paired_mobile_device_key(temp_db, monkeypatch, tmp_path):
    # Regression: these routes used to require the literal DASHBOARD_TOKEN
    # (_require_token), one tier stricter than every other bot/model-
    # management route (_require_token_or_api_key) — silently unreachable
    # by the Android app's own paired-device key, which is how it
    # authenticates everything else (see bot.bot_instances/api/bots).
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    _, plaintext = db.create_api_key("test-phone", kind="device")
    device_headers = {"X-Dashboard-Token": plaintext}

    resp = client.get("/api/providers", headers=device_headers)
    assert resp.status_code == 200

    resp = client.post(
        "/api/providers", headers=device_headers,
        json={"name": "from_mobile", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert resp.status_code == 200
    assert providers.get_provider("from_mobile") is not None

    resp = client.delete("/api/providers/from_mobile", headers=device_headers)
    assert resp.status_code == 200


def test_set_provider_route_stores_catalog_id(temp_db, monkeypatch, tmp_path):
    _isolate_provider_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.post(
        "/api/providers", headers=_headers(),
        json={"name": "my_openrouter", "base_url": "https://openrouter.ai/api/v1", "catalog_id": "openrouter"},
    )

    assert resp.status_code == 200
    assert providers.get_provider("my_openrouter")["catalog_id"] == "openrouter"

    listed = client.get("/api/providers", headers=_headers()).json()["providers"]
    assert listed[0]["catalog_id"] == "openrouter"
