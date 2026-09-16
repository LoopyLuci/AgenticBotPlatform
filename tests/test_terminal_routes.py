"""GET /api/activity and POST /api/terminal/exec — the scoped terminal
panel's only two endpoints. Built against the real FastAPI app, not a
reimplementation, so a route typo or import error fails here (same
convention as test_health_and_metrics.py).
"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from bot import activity_log
from bot.dashboard.server import build_app

_TOKEN = "test-dashboard-token"
_AUTH = {"X-Dashboard-Token": _TOKEN}


def _set_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)


def test_activity_requires_auth(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.get("/api/activity")
    assert resp.status_code == 401


def test_activity_returns_recent_log_entries(temp_db, monkeypatch):
    _set_token(monkeypatch)
    activity_log.install()
    logging.getLogger("test.activity_route").warning("route test entry")
    client = TestClient(build_app())
    resp = client.get("/api/activity", headers=_AUTH)
    assert resp.status_code == 200
    messages = [e["message"] for e in resp.json()["entries"]]
    assert "route test entry" in messages


def test_terminal_exec_requires_auth(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", json={"text": "/help"})
    assert resp.status_code == 401


def test_terminal_exec_runs_a_recognized_slash_command(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", headers=_AUTH, json={"text": "/devices"})
    assert resp.status_code == 200
    assert "no paired devices" in resp.json()["output"].lower()


def test_terminal_exec_rejects_non_slash_input(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", headers=_AUTH, json={"text": "rm -rf /"})
    assert resp.status_code == 200
    assert "not a recognized command" in resp.json()["output"].lower()


def test_terminal_exec_reports_unknown_slash_commands(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", headers=_AUTH, json={"text": "/totally_made_up_command"})
    assert resp.status_code == 200
    assert "unknown command" in resp.json()["output"].lower()


def test_terminal_exec_empty_text_returns_empty_output(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", headers=_AUTH, json={"text": "  "})
    assert resp.status_code == 200
    assert resp.json()["output"] == ""


def test_terminal_exec_rejects_unknown_instance_id(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.post("/api/terminal/exec", headers=_AUTH, json={"text": "/status", "instance_id": 999999})
    assert resp.status_code == 200
    assert "no such bot instance" in resp.json()["output"].lower()
