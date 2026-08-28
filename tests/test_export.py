"""/api/export/{table} — full-table JSON/CSV download, built against the
real FastAPI app so a route typo, whitelist bug, or SQL-injection-via-table
-name mistake fails here rather than silently in production.
"""
from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_export_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/export/jobs")
    assert resp.status_code == 401


def test_export_lists_tables(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/export/tables", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    assert "jobs" in resp.json()["tables"]


def test_export_rejects_unknown_table(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get(
        "/api/export/not_a_real_table; DROP TABLE jobs;--",
        headers={"X-Dashboard-Token": "test-token"},
    )
    assert resp.status_code == 404


def test_export_json_returns_real_rows(temp_db, monkeypatch):
    from bot import db

    now = db._now()
    temp_db.execute(
        "INSERT INTO jobs (action_type, backend, status, created_at, finished_at) VALUES ('ask', 'cli', 'success', ?, ?)",
        (now, now),
    )
    temp_db.commit()

    client = _client(monkeypatch)
    resp = client.get("/api/export/jobs?format=json", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert 'filename="jobs-' in resp.headers["content-disposition"]
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["action_type"] == "ask"


def test_export_csv_matches_json_content(temp_db, monkeypatch):
    from bot import db

    now = db._now()
    temp_db.execute(
        "INSERT INTO jobs (action_type, backend, status, created_at, finished_at) VALUES ('ask', 'cli', 'success', ?, ?)",
        (now, now),
    )
    temp_db.commit()

    client = _client(monkeypatch)
    resp = client.get("/api/export/jobs?format=csv", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["action_type"] == "ask"


def test_export_csv_empty_table_returns_header_only(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/export/swarm_runs?format=csv", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    assert resp.text == ""
