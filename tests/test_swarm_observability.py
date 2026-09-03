"""Phase 9 of the Hermes-swarm plan: the DB layer + routes backing the
hybrid swarm-observability design — job_tool_events (live top-level
SSE-derived status, see bot/swarm/observability.py) and job_children
(post-hoc structured breakdown, see bot/swarm/child_parser.py), plus the
/api/swarm-budget config routes.
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


# ------------------------------------------------------------- db: job_tool_events


def test_log_and_list_job_tool_events_in_seq_order(temp_db):
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    db.log_job_tool_event(job_id, "tool_started", "delegate_task", {"a": 1})
    db.log_job_tool_event(job_id, "tool_completed", "delegate_task", {"a": 2})

    events = db.list_job_tool_events(job_id)
    assert [e["event_type"] for e in events] == ["tool_started", "tool_completed"]
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2


def test_job_tool_events_scoped_to_their_own_job(temp_db):
    job_a = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="a")
    job_b = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="b")
    db.log_job_tool_event(job_a, "tool_started", "delegate_task", {})

    assert len(db.list_job_tool_events(job_a)) == 1
    assert len(db.list_job_tool_events(job_b)) == 0


def test_on_job_tool_event_listener_fires(temp_db):
    seen = []
    db.on_job_tool_event(lambda job_id: seen.append(job_id))
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    db.log_job_tool_event(job_id, "tool_started", "delegate_task", {})

    assert seen == [job_id]


# ---------------------------------------------------------------- db: job_children


def test_set_and_list_job_children(temp_db):
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")
    children = [
        {"index": 0, "goal": "part one", "model": "openrouter/free", "status": "ok", "result_excerpt": "done"},
        {"index": 1, "goal": "part two", "model": "openrouter/free", "status": "error", "result_excerpt": "failed"},
    ]

    db.set_job_children(job_id, children)

    rows = db.list_job_children(job_id)
    assert [r["goal"] for r in rows] == ["part one", "part two"]
    assert [r["status"] for r in rows] == ["ok", "error"]


def test_set_job_children_replaces_previous_set(temp_db):
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")
    db.set_job_children(job_id, [{"index": 0, "goal": "old", "model": "", "status": "ok", "result_excerpt": ""}])

    db.set_job_children(job_id, [{"index": 0, "goal": "new", "model": "", "status": "ok", "result_excerpt": ""}])

    rows = db.list_job_children(job_id)
    assert len(rows) == 1
    assert rows[0]["goal"] == "new"


def test_on_job_children_set_listener_fires(temp_db):
    seen = []
    db.on_job_children_set(lambda job_id: seen.append(job_id))
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    db.set_job_children(job_id, [])

    assert seen == [job_id]


# ------------------------------------------------------------------- db: misc


def test_get_latest_job_returns_most_recent_matching(temp_db):
    instance_id = 42
    db.create_job(action_type="quick_question", backend="cli", user_id=0, prompt="unrelated", instance_id=instance_id)
    first = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="first", instance_id=instance_id)
    second = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="second", instance_id=instance_id)

    row = db.get_latest_job(instance_id, "swarm_dispatch")

    assert row["id"] == second
    assert row["id"] != first


def test_get_latest_job_none_when_no_match(temp_db):
    assert db.get_latest_job(999, "swarm_dispatch") is None


def test_log_audit_returns_id_and_set_audit_log_job_id_backfills(temp_db):
    audit_id = db.log_audit(actor="dashboard", action="swarm_dispatch", detail="x")
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    db.set_audit_log_job_id(audit_id, job_id)

    rows = db.list_audit_log(actions=["swarm_dispatch"])
    assert rows[0]["job_id"] == job_id


# -------------------------------------------------------------------- routes


def test_job_tool_events_route(temp_db, monkeypatch):
    client = _client(monkeypatch)
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")
    db.log_job_tool_event(job_id, "tool_started", "delegate_task", {"x": 1})

    resp = client.get(f"/api/jobs/{job_id}/tool-events", headers=_headers())

    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 1


def test_job_children_route(temp_db, monkeypatch):
    client = _client(monkeypatch)
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")
    db.set_job_children(job_id, [{"index": 0, "goal": "x", "model": "", "status": "ok", "result_excerpt": ""}])

    resp = client.get(f"/api/jobs/{job_id}/children", headers=_headers())

    assert resp.status_code == 200
    assert len(resp.json()["children"]) == 1


def test_job_children_route_empty_before_completion(temp_db, monkeypatch):
    client = _client(monkeypatch)
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    resp = client.get(f"/api/jobs/{job_id}/children", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["children"] == []


def test_swarm_budget_config_route_defaults(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/api/swarm-budget", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["max_children"] == 6


def test_swarm_budget_config_route_updates_and_persists(temp_db, monkeypatch, tmp_path):
    from bot.config import config as config_module

    monkeypatch.setattr(config_module, "path", tmp_path / "backends.yaml")
    config_module.path.write_text("default_backend: cli\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_data", {"default_backend": "cli"})

    client = _client(monkeypatch)

    resp = client.post("/api/swarm-budget", headers=_headers(), json={"max_children": 3, "max_estimated_usd": 0.5})

    assert resp.status_code == 200
    assert resp.json()["max_children"] == 3
    assert resp.json()["max_estimated_usd"] == 0.5
