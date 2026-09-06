"""GET /api/bots/{id}/model-picker and POST /api/bots/{id}/model — the
same picker data/set-model logic Telegram's interactive /model command
already uses (bot.commands.instance_model_page/apply_instance_model),
exposed over HTTP so a non-Telegram client (the Android app's own Chat
screen) can build an equivalent native picker instead of only ever
seeing the plain-text global summary. Exercised against the real
FastAPI app via TestClient, with instance_model_groups faked so this
stays fast/offline — the real free-classification/pagination logic
already has its own dedicated coverage in test_hermes_model_discovery.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances, commands, db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_instance(model=None):
    return bot_instances.create_instance(
        name="hermes-worker", platform="telegram", backend="hermes_cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, model=model,
    )


async def _fake_groups(backend, instance_id=None):
    return [
        {"provider": "openrouter", "models": ["free-model", "paid-model"], "free_ids": {"free-model"}},
        {"provider": "another", "models": ["m1"], "free_ids": set()},
    ]


def test_model_picker_requires_auth(temp_db, monkeypatch):
    instance_id = _create_instance()
    client = _client(monkeypatch)

    resp = client.get(f"/api/bots/{instance_id}/model-picker")

    assert resp.status_code == 401


def test_model_picker_returns_provider_list_for_a_multi_provider_backend(temp_db, monkeypatch):
    monkeypatch.setattr(commands, "instance_model_groups", _fake_groups)
    instance_id = _create_instance()
    client = _client(monkeypatch)

    resp = client.get(f"/api/bots/{instance_id}/model-picker", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "providers"
    names = {p["name"] for p in body["providers"]}
    assert names == {"openrouter", "another"}


def test_model_picker_descends_into_a_chosen_provider(temp_db, monkeypatch):
    monkeypatch.setattr(commands, "instance_model_groups", _fake_groups)
    instance_id = _create_instance()
    client = _client(monkeypatch)

    resp = client.get(f"/api/bots/{instance_id}/model-picker", headers=_headers(), params={"provider": 0})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "models"
    assert body["provider"] == "openrouter"
    assert body["models"] == ["free-model", "paid-model"]


def test_model_picker_404s_for_an_unknown_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/api/bots/999999/model-picker", headers=_headers())

    assert resp.status_code == 404


def test_set_model_updates_the_instance_and_returns_a_message(temp_db, monkeypatch):
    instance_id = _create_instance()
    client = _client(monkeypatch)

    resp = client.post(f"/api/bots/{instance_id}/model", headers=_headers(), json={"model": "openrouter/free-model"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "openrouter/free-model" in body["message"]
    assert bot_instances.get_instance(instance_id)["model"] == "openrouter/free-model"


def test_set_model_requires_a_non_empty_model(temp_db, monkeypatch):
    instance_id = _create_instance()
    client = _client(monkeypatch)

    resp = client.post(f"/api/bots/{instance_id}/model", headers=_headers(), json={"model": ""})

    assert resp.status_code == 400


def test_set_model_404s_for_an_unknown_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/bots/999999/model", headers=_headers(), json={"model": "x"})

    assert resp.status_code == 404


def test_a_paired_mobile_device_can_use_the_picker_and_set_the_model(temp_db, monkeypatch):
    monkeypatch.setattr(commands, "instance_model_groups", _fake_groups)
    instance_id = _create_instance()
    client = _client(monkeypatch)
    _, plaintext = db.create_api_key("test-phone", kind="device")

    resp = client.get(f"/api/bots/{instance_id}/model-picker", headers={"X-Dashboard-Token": plaintext})
    assert resp.status_code == 200

    resp2 = client.post(f"/api/bots/{instance_id}/model", headers={"X-Dashboard-Token": plaintext}, json={"model": "openrouter/free-model"})
    assert resp2.status_code == 200
