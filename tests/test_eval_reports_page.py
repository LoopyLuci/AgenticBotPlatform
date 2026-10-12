"""Live eval reports on the results page and on the Evals screen: rendering, escaping, the read-only routes, the panel."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abp_agenteval import __main__ as cli
from abp_agenteval import page
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "docs" / "agents" / "eval-reports"
H = {"X-Dashboard-Token": "test-token"}
EVIL = '<script>alert("x")</script>&"\''


def _evil_run():
    return {"mode": "live", "model": EVIL, "when": 1791219959.0, "total": 2, "measured": 2, "passed": 1, "score": 50.0, "tokens": 10,
            "duration_ms": 61000,
            "results": [{"id": "ok_task", "title": "fine", "category": "files", "passed": True, "iterations": 2, "checks": []},
                        {"id": EVIL, "title": EVIL, "category": EVIL, "passed": False, "iterations": 3,
                         "checks": [{"name": EVIL, "ok": False, "detail": EVIL}]}]}


def _evil_compare():
    return {"mode": "live", "model": EVIL, "note": EVIL,
            "variants": [{"name": EVIL, "passed": 1, "total": 2, "score": 50.0, "tokens": 1, "iterations": 1,
                          "gained": [EVIL], "lost": [EVIL], "verdict": EVIL}]}


def test_page_renders_the_committed_reports(tmp_path):
    out = tmp_path / "p.html"
    assert cli.main(["page", "--reports", str(REPORTS), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "opencode-zen/space-bunny-free" in text and "All runs" in text and "Failed tasks" in text
    assert "Prompt comparison" in text and "name-the-search-tools" in text
    assert "71.0%" in text and "22/31" in text
    assert "http://" not in text and "https://" not in text and "<script" not in text


def test_page_escapes_everything(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps(_evil_run()), encoding="utf-8")
    (tmp_path / "cmp.json").write_text(json.dumps(_evil_compare()), encoding="utf-8")
    out = tmp_path / "p.html"
    assert page.main([], str(out), reports_dir=str(tmp_path)) == 0
    text = out.read_text(encoding="utf-8")
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_page_with_bench_and_reports_together(tmp_path):
    bench = tmp_path / "bench"
    bench.mkdir()
    out = tmp_path / "p.html"
    assert page.main([], str(out), bench_dir=str(bench), reports_dir=str(REPORTS)) == 0
    text = out.read_text(encoding="utf-8")
    assert "No complete benchmark runs yet" in text or "Leaderboard" in text


@pytest.fixture
def client(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "evil.json").write_text(json.dumps(_evil_run()), encoding="utf-8")
    (extra / "junk.json").write_text("not json", encoding="utf-8")
    (extra / "other.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
    (tmp_path / "secret.json").write_text(json.dumps(_evil_run()), encoding="utf-8")
    monkeypatch.setenv("ABP_EVAL_REPORTS_DIR", str(extra))
    return TestClient(build_app())


def test_routes_need_auth(client):
    assert client.get("/api/agent/evals/reports").status_code in (401, 403)
    assert client.get("/api/agent/evals/reports/evil.json").status_code in (401, 403)


def test_list_and_read(client):
    body = client.get("/api/agent/evals/reports", headers=H).json()
    names = {r["name"] for r in body["reports"]}
    assert "2026-10-05-space-bunny-free.json" in names and "2026-10-04-compare-space-bunny-free.json" in names and "evil.json" in names
    assert "junk.json" not in names and "other.json" not in names
    run = next(r for r in body["reports"] if r["name"] == "2026-10-05-space-bunny-free.json")
    assert run["kind"] == "run" and run["passed"] == 22 and run["tasks_failed"] == 9 and run["model_calls"] > 0
    one = client.get("/api/agent/evals/reports/evil.json", headers=H).json()
    assert one["failed"][0]["why"].startswith(EVIL) and one["categories"]
    cmp_ = client.get("/api/agent/evals/reports/2026-10-04-compare-space-bunny-free.json", headers=H).json()
    assert cmp_["kind"] == "compare" and len(cmp_["variants"]) == 2


@pytest.mark.parametrize("name", ["../secret.json", "..%2Fsecret.json", "%2e%2e%2fsecret.json", "..\\secret.json", "secret.json",
                                  "C:%5CWindows%5Cwin.ini", "%2Fetc%2Fpasswd", "evil.json%00", ".hidden.json", "evil.txt", "junk.json", "other.json"])
def test_traversal_and_unlisted_names_are_refused(client, name):
    assert client.get("/api/agent/evals/reports/" + name, headers=H).status_code in (400, 404)


# ---- the panel -------------------------------------------------------------
def test_the_panel_is_identical_in_both_uis():
    assert (ROOT / "bot/dashboard/static/evals-panel.js").read_bytes() == (ROOT / "desktop-app/ui/evals-panel.js").read_bytes()


def test_both_pages_mount_it():
    for p in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / p).read_text(encoding="utf-8")
        assert re.search(r'<section id="evals"[^>]*>', text) and 'id="ev-root"' in text and 'href="#evals"' in text
        assert re.search(r'<script src="(/static/)?evals-panel\.js"></script>', text)


def test_the_panel_escapes_and_only_calls_existing_routes(client):
    js = (ROOT / "bot/dashboard/static/evals-panel.js").read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js
    assert "E(d.model)" in js and "E(x.why" in js
    assert not re.search(r"\$\{(r|d|x|v)\.(model|name|id|title|category|why|verdict|note)\}", js), "values go through E()"
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert "/api/agent/evals/reports" in paths and "/api/agent/evals/reports/{name}" in paths
    assert set(re.findall(r"api\('(/api/[a-z/]+?)'", js)) == {"/api/agent/evals/reports", "/api/agent/evals/reports/"}
    assert client.get("/static/evals-panel.js").status_code == 200
