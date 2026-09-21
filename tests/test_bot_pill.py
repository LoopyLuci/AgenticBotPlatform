"""The top-bar pill shows how many bots are running agents right now, not a fixed "Bot online",
and the "Hot-reload armed" pill is gone."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html", "desktop-app/ui/main.js")


def _bot(name, enabled=1):
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO bot_instances (name, platform, credentials, enabled, created_at, updated_at) "
        "VALUES (?, 'telegram', '{}', ?, datetime('now'), datetime('now'))", (name, enabled)
    )
    conn.commit()
    return cur.lastrowid


def _job(instance_id, status):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO jobs (action_type, backend, status, created_at, instance_id) VALUES ('chat', 'api', ?, datetime('now'), ?)",
        (status, instance_id),
    )
    conn.commit()


def test_nothing_running_reports_zero_bots_and_how_many_are_enabled(temp_db):
    _bot("alpha")
    _bot("off", enabled=0)
    overview = db.get_overview()
    assert overview["bots_running_agents"] == 0
    assert overview["active_bots"] == []
    assert overview["bots_enabled"] == 1


def test_it_counts_bots_not_jobs(temp_db):
    a, b, c = _bot("alpha"), _bot("beta"), _bot("gamma")
    for _ in range(3):
        _job(a, "running")  # one bot with three agents working
    _job(b, "running")
    _job(b, "queued")
    _job(c, "success")  # finished work does not count
    overview = db.get_overview()
    assert overview["bots_running_agents"] == 2
    assert overview["jobs_running"] == 4
    assert overview["jobs_queued"] == 1
    assert [(x["name"], x["jobs"]) for x in overview["active_bots"]] == [("alpha", 3), ("beta", 1)]


def test_jobs_with_no_instance_count_together_as_one_default_bot(temp_db):
    _job(None, "running")
    _job(None, "running")
    overview = db.get_overview()
    assert overview["bots_running_agents"] == 1
    assert overview["active_bots"] == [{"instance_id": None, "name": "default", "jobs": 2}]


def test_the_overview_route_carries_the_new_fields(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    alpha = _bot("alpha")
    _job(alpha, "running")
    body = TestClient(build_app()).get("/api/overview", headers={"X-Dashboard-Token": "unused-dashboard-token"}).json()
    assert body["bots_running_agents"] == 1 and body["bots_enabled"] == 1
    assert body["active_bots"][0]["name"] == "alpha"


@pytest.mark.parametrize("rel", PAGES)
def test_the_fixed_labels_and_the_hot_reload_pill_are_gone(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for needle in ("Hot-reload armed", "pill-reload", "Bot online"):
        assert needle not in text, f"{rel} still has {needle!r}"


@pytest.mark.parametrize("rel", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/main.js"])
def test_both_uis_render_the_pill_from_the_overview(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "function renderBotPill(ov)" in text and "renderBotPill(ov);" in text
    assert "running agents" in text
