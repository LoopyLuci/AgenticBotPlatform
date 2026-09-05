"""/healthz (unauthenticated, like a load balancer's health probe) and
/metrics (token-gated — see the security tightening in
bot/dashboard/server.py that added auth to every GET route with no
legitimate reason to be public, once the server became reachable from
the open internet via Tailscale Funnel). Built against the real FastAPI
app (bot.dashboard.server.build_app()), not a reimplementation, so a
route typo or import error fails here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app

_TOKEN = "test-dashboard-token"
_AUTH = {"X-Dashboard-Token": _TOKEN}


def _set_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)


def test_healthz_reports_ok_with_a_working_db(temp_db):
    client = TestClient(build_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True


def test_healthz_requires_no_auth_header(temp_db):
    # Deliberately no X-Dashboard-Token — this must never 401.
    client = TestClient(build_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_metrics_requires_auth(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.get("/metrics")
    assert resp.status_code == 401


def test_metrics_is_prometheus_text_format_with_real_numbers(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.get("/metrics", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "botserver_up 1" in body
    assert "# TYPE botserver_jobs_running gauge" in body
    assert "botserver_db_size_bytes" in body


def test_metrics_reflects_real_job_counts(temp_db, monkeypatch):
    from bot import db

    _set_token(monkeypatch)
    now = db._now()
    temp_db.execute(
        "INSERT INTO jobs (action_type, backend, status, created_at, finished_at) VALUES ('ask', 'cli', 'success', ?, ?)",
        (now, now),
    )
    temp_db.commit()

    client = TestClient(build_app())
    body = client.get("/metrics", headers=_AUTH).text
    assert "botserver_jobs_completed_today 1" in body
