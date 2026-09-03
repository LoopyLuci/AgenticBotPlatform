"""Phase E of the Native Hermes-parity plan: POST /api/native-agent/{id}/dispatch
and the dispatch_native_swarm_goal MCP tool it backs — the direct-call
equivalent of api_hermes_dispatch (test_hermes_swarm_routes.py), with no
goal-prompt indirection: results come back as real structured data from
bot.agent_runtime.subagents.run_batch(), not parsed out of a reply.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances, db
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_native_agent_instance(**overrides):
    kwargs = dict(
        name="native-worker", platform="telegram", backend="native_agent",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


def _create_hermes_cli_instance():
    return bot_instances.create_instance(
        name="not-native", platform="telegram", backend="hermes_cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


def _fake_pricing(monkeypatch, catalog):
    async def fake(refresh=False):
        return catalog, "live"

    from bot import models as models_module

    monkeypatch.setattr(models_module, "custom_models_with_pricing", fake)


def test_dispatch_route_rejects_non_native_agent_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_cli_instance()

    resp = client.post(f"/api/native-agent/{instance_id}/dispatch", headers=_headers(), json={"tasks": [{"goal": "x"}]})

    assert resp.status_code == 400


def test_dispatch_route_requires_tasks(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_native_agent_instance()

    resp = client.post(f"/api/native-agent/{instance_id}/dispatch", headers=_headers(), json={})

    assert resp.status_code == 400


def test_dispatch_route_runs_tasks_and_populates_job_children(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_native_agent_instance()

    async def fake_run_batch(tasks, *, role, provider, model, max_children, parent_instance_id):
        return [
            {"index": i, "goal": t["goal"], "model": f"{provider}/{model}", "status": "ok", "result_excerpt": f"done {i}"}
            for i, t in enumerate(tasks)
        ]

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", fake_run_batch)

    resp = client.post(
        f"/api/native-agent/{instance_id}/dispatch", headers=_headers(),
        json={"tasks": [{"goal": "part one"}, {"goal": "part two"}], "worker_provider": "ollama", "worker_model": "llama3.1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["worker_provider"] == "ollama"
    assert len(body["children"]) == 2
    job_id = body["job_id"]
    assert job_id is not None

    children = db.list_job_children(job_id)
    assert len(children) == 2
    assert children[0]["goal"] == "part one"

    job = db.get_job(job_id)
    assert job["status"] == "success"
    assert job["backend"] == "native_agent"


def test_dispatch_route_auto_picks_free_model(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_native_agent_instance()
    _fake_pricing(monkeypatch, {
        "openrouter": [
            {"id": "paid-model", "free": False, "input": 1e-6, "output": 2e-6},
            {"id": "free-model", "free": True, "input": 0.0, "output": 0.0},
        ],
    })

    async def fake_run_batch(tasks, *, role, provider, model, max_children, parent_instance_id):
        assert provider == "openrouter"
        assert model == "free-model"
        return [{"index": 0, "goal": tasks[0]["goal"], "model": f"{provider}/{model}", "status": "ok", "result_excerpt": "ok"}]

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", fake_run_batch)

    resp = client.post(
        f"/api/native-agent/{instance_id}/dispatch", headers=_headers(), json={"tasks": [{"goal": "x"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["worker_model"] == "free-model"


def test_dispatch_route_blocked_by_budget_never_calls_run_batch(temp_db, monkeypatch):
    from bot.config import config as config_singleton

    client = _client(monkeypatch)
    instance_id = _create_native_agent_instance()
    monkeypatch.setattr(config_singleton, "_data", {"swarm_budget": {"enabled": True, "max_children": 1}})
    _fake_pricing(monkeypatch, {})

    async def fake_run_batch(*args, **kwargs):
        raise AssertionError("run_batch should never be called when the budget guard refuses")

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", fake_run_batch)

    resp = client.post(
        f"/api/native-agent/{instance_id}/dispatch", headers=_headers(),
        json={"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]},
    )

    assert resp.status_code == 400
    blocked = db.list_audit_log(actions=["swarm_dispatch_blocked"])
    assert len(blocked) == 1


def test_dispatch_route_failure_marks_job_failed(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_native_agent_instance()
    _fake_pricing(monkeypatch, {})

    from bot.backends.base import BackendError

    async def fake_run_batch(*args, **kwargs):
        raise BackendError("boom")

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", fake_run_batch)

    resp = client.post(
        f"/api/native-agent/{instance_id}/dispatch", headers=_headers(),
        json={"tasks": [{"goal": "x"}], "worker_provider": "ollama", "worker_model": "m"},
    )

    assert resp.status_code == 502
