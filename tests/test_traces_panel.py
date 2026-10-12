"""The Traces screen: read-only routes over the agent trace store, and a panel that ships identically in both UIs."""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.agent_runtime import trace
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Dashboard-Token": "test-token"}
SECRET = "sk-ant-api03-" + "A" * 40


@pytest.fixture
def client(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "traces.db"))
    trace._stores.clear()
    store = trace.get_store()
    now = time.time()

    def run(rid, ago, *, agent="main", instance=1, parent=None, status="ok", model="m-1", tool="read_file", target="a.txt",
            end=True, error=""):
        t = now - ago
        start = {"agent": agent, "model": model, "transport": "api", "instance_id": instance, "session": "s1", "tools": 5}
        if parent:
            start["parent_run"] = parent
        store.append("agent.run.start", start, run_id=rid, ts=t)
        store.append("llm.call", {"iteration": 1, "model": model, "duration_ms": 400, "tokens": 120, "tool_calls": 1}, run_id=rid, ts=t + 0.5)
        store.append("approval", {"tool": tool, "decision": "allow", "reason": "rule"}, run_id=rid, ts=t + 0.6)
        store.append("tool.call", {"tool": tool, "status": "failed" if status == "failed" else "ok", "duration_ms": 30,
                                   "target": target, "arg_chars": 10, "output_chars": 99, "error": error}, run_id=rid, ts=t + 0.7)
        store.append("llm.call", {"iteration": 2, "model": model, "duration_ms": 300, "tokens": 80, "tool_calls": 0}, run_id=rid, ts=t + 1.2)
        if end:
            store.append("agent.run.end", {"status": status, "duration_ms": 1500, "iterations": 2, "tokens": 200, "error": error},
                         run_id=rid, ts=t + 1.5)

    run("aaaa0001", 300, instance=1)
    run("bbbb0002", 200, instance=2, status="failed", tool="run_shell", target="make deploy", error="exit 2")
    run("cccc0003", 100, agent="researcher", end=False)
    run("dddd0004", 90, agent="sub", parent="cccc0003", target="child.txt")
    run("eeee0005", 3 * 86400, instance=1)
    yield TestClient(build_app())
    trace._stores.clear()


def ids(body):
    return [r["run_id"] for r in body["runs"]]


def test_runs_come_newest_first_with_totals(client):
    assert client.get("/api/agent/traces").status_code == 401
    body = client.get("/api/agent/traces", headers=H).json()
    assert ids(body) == ["dddd0004", "cccc0003", "bbbb0002", "aaaa0001", "eeee0005"] and body["total"] == 5
    first = {r["run_id"]: r for r in body["runs"]}
    assert first["aaaa0001"]["status"] == "ok" and first["aaaa0001"]["tokens"] == 200 and first["aaaa0001"]["llm_calls"] == 2
    assert first["bbbb0002"]["failed_tools"] == 1 and first["cccc0003"]["status"] == "running"
    assert first["dddd0004"]["parent_run"] == "cccc0003"
    assert {"ok", "failed", "running"} <= set(body["statuses"])


def test_filters_and_paging(client):
    def get(**p):
        return client.get("/api/agent/traces", params=p, headers=H).json()

    assert ids(get(status="failed")) == ["bbbb0002"]
    assert ids(get(status="running")) == ["cccc0003"]
    assert ids(get(instance_id=2)) == ["bbbb0002"]
    assert ids(get(q="make deploy")) == ["bbbb0002"]
    assert ids(get(q="RESEARCHER")) == ["cccc0003"]
    assert ids(get(q="child.txt")) == ["dddd0004"]
    assert ids(get(q="aaaa0001")) == ["aaaa0001"]
    assert get(q="100%_nothing")["total"] == 0
    assert "eeee0005" not in ids(get(since=time.time() - 86400))
    assert ids(get(until=time.time() - 86400)) == ["eeee0005"]
    page = get(limit=2, offset=2)
    assert ids(page) == ["bbbb0002", "aaaa0001"] and page["total"] == 5 and page["offset"] == 2
    assert client.get("/api/agent/traces", params={"limit": 500}, headers=H).status_code == 422


def test_run_detail_has_ordered_events_totals_and_links(client):
    body = client.get("/api/agent/traces/cccc0003", headers=H).json()
    assert body["run"]["status"] == "running" and body["parent"] is None
    assert [c["run_id"] for c in body["children"]] == ["dddd0004"]
    child = client.get("/api/agent/traces/dddd0004", headers=H).json()
    assert child["parent"]["run_id"] == "cccc0003" and child["children"] == []
    ev = client.get("/api/agent/traces/bbbb0002", headers=H).json()
    assert [e["kind"] for e in ev["events"]] == ["agent.run.start", "llm.call", "approval", "tool.call", "llm.call", "agent.run.end"]
    assert [e["offset_ms"] for e in ev["events"]] == sorted(e["offset_ms"] for e in ev["events"])
    assert ev["totals"] == {"llm_calls": 2, "tool_calls": 1, "tokens": 200, "usd": None, "cache_read": 0, "cache_create": 0, "llm_ms": 700,
                            "tool_ms": 30, "failed_tools": 1, "denied_tools": 0, "approvals": 1}
    assert ev["run"]["error"] == "exit 2"
    tool = next(e for e in ev["events"] if e["kind"] == "tool.call")["data"]
    assert tool["target"] == "make deploy" and tool["output_chars"] == 99 and "output" not in tool


def test_missing_and_malformed_ids(client):
    assert client.get("/api/agent/traces/nope", headers=H).status_code == 404
    assert client.get("/api/agent/traces/a%20b%27--", headers=H).status_code == 400


def test_routes_never_return_a_secret_pasted_into_a_target(client):
    store = trace.get_store()
    store.append("agent.run.start", {"agent": "x", "model": "m"}, run_id="ffff0006")
    store.append("tool.call", {"tool": "run_shell", "status": "ok", "target": f"curl -H 'Authorization: Bearer {SECRET}'"}, run_id="ffff0006")
    assert SECRET not in client.get("/api/agent/traces/ffff0006", headers=H).text
    assert SECRET not in client.get("/api/agent/traces", headers=H).text


def test_an_empty_store_is_an_empty_list(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "none.db"))
    trace._stores.clear()
    body = TestClient(build_app()).get("/api/agent/traces", headers=H).json()
    assert body["runs"] == [] and body["total"] == 0
    trace._stores.clear()


# ---- the panel -------------------------------------------------------------
def test_the_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static/traces-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/traces-panel.js").read_text(encoding="utf-8")
    assert dash == desk


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="traces"[^>]*>', text) and 'id="tr-root"' in text and 'href="#traces"' in text
        assert re.search(r'<script src="(/static/)?traces-panel\.js"></script>', text)


def test_the_panel_shows_what_the_screen_promises():
    js = (ROOT / "bot/dashboard/static/traces-panel.js").read_text(encoding="utf-8")
    for needle in ("Timeline", "Sub-agent runs", "Started by run", "No runs match", "Refresh", "Previous", "Next", "toLocaleString", "E(d.target)"):
        assert needle in js, needle
    assert "http://" not in js and "https://" not in js, "no CDN or external address"
    assert not re.search(r"\$\{(r|d)\.(agent|model|error|message|target|tool|reason)\}", js), "values go through E()"


def test_the_panel_only_calls_routes_that_exist(client):
    paths = {getattr(r, "path", "") for r in client.app.routes}
    js = (ROOT / "bot/dashboard/static/traces-panel.js").read_text(encoding="utf-8")
    called = set(re.findall(r"api\('(/api/[a-z/]+?)(?:'|\?)", js))
    assert called == {"/api/agent/traces", "/api/agent/traces/"}
    assert "/api/agent/traces" in paths and "/api/agent/traces/{run_id}" in paths
    assert client.get("/static/traces-panel.js").status_code == 200
