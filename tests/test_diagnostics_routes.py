"""GET /api/diagnostics/* — the Diagnostics tab's backing routes. Built
against the real FastAPI app (same convention as test_terminal_routes.py)
so a route typo or import error fails here.
"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from bot import diagnostics
from bot.dashboard.server import build_app

_TOKEN = "test-dashboard-token"
_AUTH = {"X-Dashboard-Token": _TOKEN}


def _set_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)


def test_diagnostics_summary_requires_auth(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.get("/api/diagnostics/summary")
    assert resp.status_code == 401


def test_diagnostics_summary_returns_system_info_and_telemetry(temp_db, monkeypatch):
    _set_token(monkeypatch)
    client = TestClient(build_app())
    resp = client.get("/api/diagnostics/summary", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "app_version" in body["system_info"]
    assert "counters" in body["telemetry"]
    assert "crash_report_count" in body


def test_diagnostics_crash_reports_list_and_detail_round_trip(temp_db, monkeypatch, tmp_path):
    _set_token(monkeypatch)
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    record = logging.LogRecord(
        name="test.route", level=logging.CRITICAL, pathname=__file__, lineno=1,
        msg="route crash", args=(), exc_info=None,
    )
    report_id = diagnostics.write_crash_report(record)

    client = TestClient(build_app())
    list_resp = client.get("/api/diagnostics/crash-reports", headers=_AUTH)
    assert list_resp.status_code == 200
    assert any(r["id"] == report_id for r in list_resp.json()["reports"])

    detail_resp = client.get(f"/api/diagnostics/crash-reports/{report_id}", headers=_AUTH)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["message"] == "route crash"


def test_diagnostics_crash_report_detail_404s_for_unknown_id(temp_db, monkeypatch, tmp_path):
    _set_token(monkeypatch)
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    client = TestClient(build_app())
    resp = client.get("/api/diagnostics/crash-reports/does-not-exist", headers=_AUTH)
    assert resp.status_code == 404


def test_diagnostics_bundle_downloads_a_zip(temp_db, monkeypatch, tmp_path):
    _set_token(monkeypatch)
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    monkeypatch.setattr(diagnostics, "BUNDLE_DIR", tmp_path / "support_bundles")
    monkeypatch.setattr(diagnostics, "LOG_DIR", tmp_path)

    client = TestClient(build_app())
    resp = client.get("/api/diagnostics/bundle", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
