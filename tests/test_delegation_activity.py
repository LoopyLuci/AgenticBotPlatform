"""GET /api/delegation-activity and bot.db.list_audit_log() — surfaces
recent ask_instance/dispatch_swarm_goal/delegate_to_instance events (a
"source -> target: prompt" audit_log row each of the three already
writes) so the dashboard can show "who asked whom to do what" without
wading through every other audit event.
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


# ------------------------------------------------------------- db.list_audit_log


def test_list_audit_log_filters_to_given_actions(temp_db):
    db.log_audit(actor="agent:a", action="agent_ask", detail="-> b: hi")
    db.log_audit(actor="dashboard", action="config_reload", detail="")
    db.log_audit(actor="agent:c", action="swarm_dispatch", detail="-> d: goal")

    rows = db.list_audit_log(actions=["agent_ask", "swarm_dispatch"])

    actions = {r["action"] for r in rows}
    assert actions == {"agent_ask", "swarm_dispatch"}
    assert len(rows) == 2


def test_list_audit_log_newest_first(temp_db):
    db.log_audit(actor="a", action="agent_ask", detail="first")
    db.log_audit(actor="a", action="agent_ask", detail="second")

    rows = db.list_audit_log(actions=["agent_ask"])

    assert rows[0]["detail"] == "second"
    assert rows[1]["detail"] == "first"


def test_list_audit_log_respects_limit(temp_db):
    for i in range(5):
        db.log_audit(actor="a", action="agent_ask", detail=str(i))

    rows = db.list_audit_log(actions=["agent_ask"], limit=2)

    assert len(rows) == 2


def test_list_audit_log_no_filter_returns_everything(temp_db):
    db.log_audit(actor="a", action="agent_ask", detail="x")
    db.log_audit(actor="a", action="config_reload", detail="y")

    rows = db.list_audit_log()

    assert len(rows) == 2


# ----------------------------------------------------------- dashboard route


def test_delegation_activity_route_shows_only_delegation_actions(temp_db, monkeypatch):
    client = _client(monkeypatch)
    db.log_audit(actor="agent:manager", action="agent_ask", detail="-> worker: do the thing")
    db.log_audit(actor="dashboard", action="config_reload", detail="")
    db.log_audit(actor="agent:manager", action="swarm_dispatch", detail="-> openrouter/free: goal")
    db.log_audit(actor="agent:manager", action="agent_delegate", detail="-> worker: another thing")

    resp = client.get("/api/delegation-activity", headers=_headers())

    assert resp.status_code == 200
    events = resp.json()["events"]
    actions = {e["action"] for e in events}
    assert actions == {"agent_ask", "swarm_dispatch", "agent_delegate"}
    assert len(events) == 3


def test_delegation_activity_route_respects_limit(temp_db, monkeypatch):
    client = _client(monkeypatch)
    for i in range(10):
        db.log_audit(actor="agent:manager", action="agent_ask", detail=f"-> worker: task {i}")

    resp = client.get("/api/delegation-activity?limit=3", headers=_headers())

    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 3


def test_delegation_activity_route_clamps_absurd_limit(temp_db, monkeypatch):
    client = _client(monkeypatch)
    db.log_audit(actor="agent:manager", action="agent_ask", detail="-> worker: hi")

    resp = client.get("/api/delegation-activity?limit=99999", headers=_headers())

    assert resp.status_code == 200  # doesn't error, just clamps server-side


def test_delegation_activity_empty_by_default(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/api/delegation-activity", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["events"] == []
