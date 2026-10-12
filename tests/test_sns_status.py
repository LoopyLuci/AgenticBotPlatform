"""The Sandbox screen: GET /api/sandbox/summary over a real process in a real cell, and a panel identical in both UIs."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app
from bot.sandbox_ns import guard, registry as registry_mod
from bot.sandbox_ns.cell import cell_for
from bot.sandbox_ns.spawn import spawn

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Dashboard-Token": "test-token"}
SECRET = "sk-ant-api03-" + "B" * 30


@pytest.fixture
def client(monkeypatch, tmp_path):
    guard.install()
    monkeypatch.setattr(registry_mod.registry, "_path", tmp_path / "sandbox_ns" / "live.json")
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_summary_needs_the_dashboard_token(client):
    assert client.get("/api/sandbox/summary").status_code == 401
    assert client.get("/api/sandbox/summary", headers={"X-Dashboard-Token": "nope"}).status_code == 401


def test_the_summary_describes_a_live_cell_without_its_arguments(client):
    with cell_for("agent", name="status test", owner="tests.sns_status") as cell:
        proc = spawn([sys.executable, "-c", "import time; time.sleep(60)", "--api-key", SECRET], cell=cell,
                     owner="tests.sns_status", name="sleeper")
        try:
            body = client.get("/api/sandbox/summary", headers=H).json()
            assert {"now", "run_id", "guard", "capabilities", "cells", "processes", "call_sites", "reflexes"} <= set(body)
            mine = next(c for c in body["cells"] if c["id"] == cell.id)
            assert mine["call_site"] == "tests.sns_status" and mine["preset"] == "agent"
            assert mine["killable"] is True and mine["age_s"] >= 0 and mine["processes"] >= 1
            row = next(p for p in body["processes"] if p["pid"] == proc.pid)
            assert row["call_site"] == "tests.sns_status" and row["preset"] == "agent" and row["cell"] == cell.id
            assert row["exe"].lower().startswith("python") and "argv" not in row and "cwd" not in row
            assert body["call_sites"]["tests.sns_status"] >= 1
            caps = body["capabilities"]
            assert caps["os"] in ("windows", "linux", "macos") and "containment" in caps and "avoid_cpus" in caps
            assert SECRET not in client.get("/api/sandbox/summary", headers=H).text
        finally:
            cell.kill("test over")
            proc.wait(timeout=30)


def test_a_killed_cell_shows_up_as_a_recent_reflex_action(client):
    with cell_for("tool", name="to stop", owner="tests.sns_status") as cell:
        proc = spawn([sys.executable, "-c", "import time; time.sleep(60)"], cell=cell, owner="tests.sns_status")
        res = client.post(f"/api/sandbox/cells/{cell.id}/kill", headers=H)
        assert res.status_code == 200
        proc.wait(timeout=30)
        body = client.get("/api/sandbox/summary", headers=H).json()
        assert any(e["kind"] == "kill" and e.get("cell") == cell.id for e in body["reflexes"]), body["reflexes"]


def test_a_daemon_cell_is_not_offered_a_kill(client):
    with cell_for("daemon", name="svc", owner="tests.sns_status") as cell:
        body = client.get("/api/sandbox/summary", headers=H).json()
        mine = next(c for c in body["cells"] if c["id"] == cell.id)
        assert mine["persistent"] is True and mine["killable"] is False


# ---- the panel -------------------------------------------------------------
def test_the_panel_is_identical_in_both_uis():
    assert (ROOT / "bot/dashboard/static/sns-panel.js").read_bytes() == (ROOT / "desktop-app/ui/sns-panel.js").read_bytes()


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="sns"[^>]*>', text) and 'id="sns-root"' in text and 'href="#sns"' in text
        assert re.search(r'<script src="(/static/)?sns-panel\.js"></script>', text)


def test_the_panel_escapes_values_and_confirms_before_a_kill(client):
    js = (ROOT / "bot/dashboard/static/sns-panel.js").read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js
    assert "window.confirm(" in js and "method: 'POST'" in js
    assert not re.search(r"\$\{(c|p|e|d)\.(call_site|name|exe|preset|detail|kind|cell|notes?)\}", js), "values go through E()"
    called = set(re.findall(r"api\('(/api/[a-z/]+?)(?:'|\?)", js))
    assert "/api/sandbox/summary" in called
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert "/api/sandbox/summary" in paths and "/api/sandbox/cells/{cell_id}/kill" in paths
    assert client.get("/static/sns-panel.js").status_code == 200
