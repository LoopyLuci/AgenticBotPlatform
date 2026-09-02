"""Phases 2-3 of the Hermes-swarm plan: the new /api/hermes/{id}/delegation
and /api/hermes/{id}/dispatch routes, and bot/hermes_config.py's
comment-preserving read/write of Hermes's own ~/.hermes/config.yaml.

Exercised against the real FastAPI app via TestClient (matching
test_schedule_routes.py's precedent) with router.ask() and the Hermes
config file itself faked — this is about BotServer's own routing/config
logic, not a live Hermes process or a real home-directory file.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances, hermes_config
from bot.backends.base import BackendResult
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_hermes_instance(**overrides):
    kwargs = dict(
        name="hermes-worker", platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


def _create_cli_instance():
    return bot_instances.create_instance(
        name="cli-bot", platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


# --------------------------------------------------------------- hermes_config


def test_read_delegation_config_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")
    assert hermes_config.read_delegation_config() == {}


def test_set_delegation_config_creates_file_and_returns_delegation(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    result = hermes_config.set_delegation_config(provider="openrouter", model="some/model:free", max_concurrent_children=4)

    assert result == {"provider": "openrouter", "model": "some/model:free", "max_concurrent_children": 4}
    assert path.is_file()
    assert hermes_config.read_delegation_config() == result


def test_set_delegation_config_preserves_unrelated_keys_and_comments(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# a real user comment that must survive\n"
        "model: claude-opus-4.8\n"
        "delegation:\n"
        "  max_spawn_depth: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    hermes_config.set_delegation_config(provider="openrouter")

    text = path.read_text(encoding="utf-8")
    assert "a real user comment that must survive" in text
    assert "model: claude-opus-4.8" in text
    delegation = hermes_config.read_delegation_config()
    assert delegation["provider"] == "openrouter"
    assert delegation["max_spawn_depth"] == 2  # untouched key survives the merge


def test_set_delegation_config_no_op_with_no_changes(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    result = hermes_config.set_delegation_config()

    assert result == {}
    assert not path.exists()  # never created a file for a no-op call


# --------------------------------------------------------------------- routes


def test_delegation_route_rejects_non_hermes_gateway_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_cli_instance()

    resp = client.post(f"/api/hermes/{instance_id}/delegation", headers=_headers(), json={"provider": "openrouter"})

    assert resp.status_code == 400


def test_delegation_route_sets_and_reads_back(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"provider": "openrouter", "model": "meta-llama/llama-3.1-8b-instruct:free"},
    )
    assert resp.status_code == 200
    assert resp.json()["delegation"]["provider"] == "openrouter"

    resp = client.get(f"/api/hermes/{instance_id}/delegation", headers=_headers())
    assert resp.json()["delegation"]["model"] == "meta-llama/llama-3.1-8b-instruct:free"


def test_delegation_route_refuses_auto_approve_without_confirm(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"subagent_auto_approve": True},
    )

    assert resp.status_code == 400
    assert hermes_config.read_delegation_config() == {}


def test_delegation_route_allows_auto_approve_with_confirm(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"subagent_auto_approve": True, "confirm": True},
    )

    assert resp.status_code == 200
    assert resp.json()["delegation"]["subagent_auto_approve"] is True


def test_dispatch_route_requires_goal(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()

    resp = client.post(f"/api/hermes/{instance_id}/dispatch", headers=_headers(), json={})

    assert resp.status_code == 400


def test_dispatch_route_auto_picks_free_model_and_dispatches(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    async def fake_pricing(iid, refresh=False):
        return (
            {
                "openrouter": [
                    {"id": "paid-model", "free": False, "input": "$1", "output": "$2"},
                    {"id": "free-model", "free": True, "input": None, "output": None},
                ],
            },
            "live",
        )

    from bot import models as models_module

    monkeypatch.setattr(models_module, "hermes_models_with_pricing", fake_pricing)

    sent_prompts = []

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        sent_prompts.append(prompt)
        return BackendResult(text="all subtasks done", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    resp = client.post(f"/api/hermes/{instance_id}/dispatch", headers=_headers(), json={"goal": "build the thing"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "all subtasks done"
    assert body["worker_provider"] == "openrouter"
    assert body["worker_model"] == "free-model"
    assert "build the thing" in sent_prompts[0]
    assert "delegate_task" in sent_prompts[0]
    # The auto-picked free model was actually written as the delegation default.
    assert hermes_config.read_delegation_config()["model"] == "free-model"


def test_dispatch_route_respects_explicit_worker_model(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        return BackendResult(text="ok", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    resp = client.post(
        f"/api/hermes/{instance_id}/dispatch", headers=_headers(),
        json={"goal": "do it", "worker_provider": "ollama", "worker_model": "llama3:free"},
    )

    assert resp.status_code == 200
    assert resp.json()["worker_provider"] == "ollama"
    assert resp.json()["worker_model"] == "llama3:free"
    assert hermes_config.read_delegation_config()["provider"] == "ollama"
