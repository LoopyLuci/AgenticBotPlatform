"""LF5: the manager channel (bot/manager) - the escalation queue, the capped digest, the capacity-gated check-ins, token accounting."""
from __future__ import annotations

import json
import time

import pytest

from bot import coordination, manager
from bot.capacity import governor
from bot.conductor import store as cstore
from bot.manager import checkin, digest, escalations
from bot.manager import store as mstore


@pytest.fixture(autouse=True)
def clean():
    from bot.capacity import events

    governor._last_seen.clear()
    events._ring.clear()
    digest._cache.clear()
    yield
    digest._cache.clear()


def _task(title: str, final: str, note: str = "", key: str = ""):
    b = coordination.get_board()
    t = b.create_task("abp/test", title, key=key or title.replace(" ", "-"))[0]
    fence = b.claim(t["id"], "w1")["fence"]
    b.set_state(t["id"], "w1", fence, "running")
    b.set_state(t["id"], "w1", fence, final, note=note)
    return t


def _escalate(plan="p1", key="t1", diff="x" * 4000):
    cstore.escalate(plan, key, "rejected twice", {"title": "Add the flag", "branch": "task/t1", "report": {"findings": [
        {"rule": "no-feature-removed", "message": "route removed", "file": "bot/a.py", "line": 12}]},
        "blocked": {"kind": "rejections", "question": "Is the reviewer right?", "failing_check": "no-feature-removed: route removed",
                    "diff_excerpt": diff}})


def _queue_entry():
    return cstore.enqueue({"id": "i1", "plan_id": "p1", "branch": "integ/agents-1", "head_sha": "a" * 40, "merged": ["t1"], "flaky": []})


# ---- escalation queue ---------------------------------------------------------------------------------------------
def test_queue_aggregates_every_source():
    _escalate()
    _queue_entry()
    _task("Stuck thing", "blocked", note="needs a decision")
    governor.set_override("local-only", reason="usage cap", ttl_s=3600)
    view = escalations.view()
    kinds = view["by_kind"]
    assert kinds.get("rejections") == 1 and kinds.get("push_approval") == 1
    assert kinds.get("blocked") == 1 and kinds.get("capacity_override") == 1
    assert view["count"] == 4
    esc = next(i for i in view["items"] if i["kind"] == "rejections")
    assert esc["where"] == "bot/a.py:12" and esc["question"] and "unblock" in esc["decide"]
    assert len(esc["diff_excerpt"]) == escalations.DIFF_CAP


def test_conductor_blocked_task_is_not_listed_twice():
    _escalate(plan="p9", key="k9")
    b = coordination.get_board()
    t = b.create_task("abp/swarm-conductor-p9", "Add the flag", key="k9")[0]
    fence = b.claim(t["id"], "w1")["fence"]
    b.set_state(t["id"], "w1", fence, "running")
    b.set_state(t["id"], "w1", fence, "blocked", note="ESCALATED")
    assert escalations.view()["count"] == 1


def test_repeated_failure_needs_two_attempts():
    t = _task("Flaky job", "failed", note="boom")
    b = coordination.get_board()
    b._conn().execute("UPDATE coord_tasks SET attempts = 1 WHERE id = ?", (t["id"],))
    b._conn().commit()
    assert escalations.view()["count"] == 0                        # one failure is just a failure
    row = b._conn().execute("SELECT state FROM coord_tasks WHERE id = ?", (t["id"],)).fetchone()
    b._conn().execute("UPDATE coord_tasks SET attempts = 2, state = 'failed' WHERE id = ?", (t["id"],))
    b._conn().commit()
    assert row is not None
    items = escalations.view()["items"]
    assert [i["kind"] for i in items] == ["repeated_failure"] and "2 times" in items[0]["question"]


def test_resolved_escalation_leaves_the_queue():
    _escalate()
    cstore.resolve_escalation(cstore.list_escalations()[0]["id"])
    assert escalations.view()["count"] == 0


# ---- digest -------------------------------------------------------------------------------------------------------
def test_digest_contents_and_no_raw_logs():
    _task("Did the thing", "done")
    _task("Broke the thing", "failed", note="E" * 900)
    _escalate()
    doc = digest.build(since=0)
    assert doc["finished_total"] == 1 and doc["failed_total"] == 1
    assert len(doc["failed"][0]["why"]) <= 160
    assert doc["escalations"]["count"] == 1
    assert len(doc["escalations"]["items"][0]["diff_excerpt"]) <= 400
    assert doc["capacity"]["state"] == "cloud-led"
    assert "E" * 200 not in json.dumps(doc)
    assert "NEEDS A DECISION" in digest.to_text(doc)


def test_digest_size_cap_holds_under_a_flood(monkeypatch):
    for i in range(40):
        _task(f"Finished number {i} " + "w" * 60, "done")
        _task(f"Failed number {i} " + "w" * 40, "failed", note="x" * 300)
    for i in range(25):
        _escalate(plan=f"p{i}", key=f"k{i}", diff="d" * 1500)
    doc = digest.build(since=0)
    assert doc["tokens"] <= doc["cap_tokens"] == 3000
    assert manager.est_tokens(json.dumps(doc, separators=(",", ":"))) <= 3000
    assert doc["escalations"]["count"] == 25 and doc["truncated"]            # counts survive, detail is cut and says so
    monkeypatch.setattr(manager, "settings", lambda: {"digest": {"cap_tokens": 400, "cache_s": 0, "diff_chars": 400},
                                                      "checkins": manager.DEFAULTS["checkins"]})
    small = digest.build(since=0, fresh=True)
    assert small["tokens"] <= 400 and small["escalations"]["count"] == 25
    assert len(digest.to_text(doc)) <= 3500


def test_digest_is_cached_then_fresh_on_demand():
    first = digest.build(since=0)
    _task("Late arrival", "done")
    again = digest.build(since=0)
    assert again.get("cached") and again["finished_total"] == first["finished_total"]
    assert digest.build(since=0, fresh=True)["finished_total"] == first["finished_total"] + 1


def test_since_marker_moves_only_when_marked():
    _task("Before", "done")
    peek = digest.build(mark=False)
    assert peek["finished_total"] == 1 and mstore.load()["last_digest_ts"] == 0
    marked = digest.build(mark=True)
    assert mstore.load()["last_digest_ts"] == marked["generated"]
    time.sleep(1.1)
    _task("After", "done")
    nxt = digest.build(fresh=True)
    assert [t["title"] for t in nxt["finished"]] == ["After"]
    assert digest.build(since=0, fresh=True)["finished_total"] == 2          # an explicit since overrides the marker


def test_capacity_events_appear_since_the_marker():
    governor.set_override("local-only", reason="usage cap", ttl_s=60)
    doc = digest.build(since=0)
    assert doc["capacity"]["state"] == "local-only" and doc["capacity"]["override"]["state"] == "local-only"
    assert any("capacity.changed" in e for e in doc["events"])
    assert digest.build(since=time.time() + 5, fresh=True)["events"] == []


# ---- check-ins ----------------------------------------------------------------------------------------------------
def test_checkin_is_queued_while_local_only_and_runs_when_capacity_returns():
    _task("Done while away", "done")
    governor.set_override("local-only", reason="usage cap", ttl_s=3600)
    res = checkin.run_once()
    assert res["status"] == "queued" and mstore.load()["queued"] and mstore.load()["digests"] == 0   # nothing built, no tokens
    governor.clear_override()
    res = checkin.run_once()
    assert res["status"] == "done" and res["digest"]["finished_total"] == 1
    mem = mstore.load()
    assert mem["queued"] is None and mem["checkin_count"] == 1 and mem["latest"]


def test_hybrid_still_checks_in():
    _task("Work", "done")
    governor.set_override("hybrid", reason="rate limits", ttl_s=60)
    assert checkin.run_once()["status"] == "done"


def test_quiet_checkin_costs_nothing():
    res = checkin.run_once()
    assert res["status"] == "quiet" and mstore.load()["tokens_total"] == 0 and mstore.load()["digests"] == 0


def test_checkins_are_off_by_default_and_due_follows_config(monkeypatch):
    assert manager.DEFAULTS["checkins"]["enabled"] is False and checkin.due() is False
    on = {"digest": manager.DEFAULTS["digest"], "checkins": {"enabled": True, "interval_s": 3600, "poll_s": 60}}
    monkeypatch.setattr(manager, "settings", lambda: on)
    assert checkin.due() is True                                   # never ran
    mstore.update(last_checkin_ts=time.time())
    assert checkin.due() is False
    governor.set_override("local-only", reason="cap", ttl_s=3600)
    checkin.run_once()
    governor.clear_override()
    assert checkin.due() is True                                   # queued while away: due at once, interval or not


# ---- token accounting ---------------------------------------------------------------------------------------------
def test_token_accounting_counts_delivered_digests_only():
    _task("Work", "done")
    digest.build(since=0, mark=False)
    assert mstore.load()["tokens_total"] == 0                       # a peek is not delivered
    d1 = digest.build(since=0, mark=True, fresh=True)
    assert mstore.load()["tokens_total"] == d1["tokens"] > 0
    assert d1["accounting"]["manager_tokens_total"] == d1["tokens"]
    _task("More", "done")
    res = checkin.run_once()
    mem = mstore.load()
    assert mem["tokens_total"] == d1["tokens"] + res["tokens"] and mem["checkins"][-1]["tokens"] == res["tokens"]
    assert res["digest"]["accounting"]["checkins"] == 1


def test_factory_tokens_come_from_agent_traces():
    from bot.agent_runtime import trace

    st = trace.get_store()
    st.append("llm.call", {"iteration": 1, "model": "modelmistress/dusty", "tokens": 700}, run_id="r1")
    st.append("llm.call", {"iteration": 2, "model": "anthropic/claude", "tokens": 300}, run_id="r1")
    f = digest.factory_tokens(0)
    assert f["tokens"] == 1000 and f["local_tokens"] == 700 and f["llm_calls"] == 2
    assert digest.build(since=0)["accounting"]["factory_tokens_since_digest"] == 1000


# ---- API and MCP --------------------------------------------------------------------------------------------------
def test_the_api_serves_digest_escalations_and_checkin(monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/manager/digest").status_code == 401 and c.post("/api/manager/checkin", json={}).status_code == 401
    _task("Done", "done")
    _escalate()
    d = c.get("/api/manager/digest", headers=h, params={"since": 0, "text": 1}).json()
    assert d["finished_total"] == 1 and d["tokens"] <= 3000 and "factory digest" in d["text"]
    assert mstore.load()["digests"] == 0                           # GET does not mark unless asked
    assert c.get("/api/manager/digest", headers=h, params={"mark": 1}).json()["accounting"]["digests_delivered"] == 1
    e = c.get("/api/manager/escalations", headers=h).json()
    assert e["count"] == 1 and e["items"][0]["where"] == "bot/a.py:12"
    assert c.get("/api/manager/checkin", headers=h).json()["enabled"] is False
    assert c.post("/api/manager/checkin", headers=h, json={"force": True}).json()["status"] == "done"


def test_the_mcp_tools_exist():
    from bot import mcp_server

    assert callable(mcp_server.manager_digest.fn if hasattr(mcp_server.manager_digest, "fn") else mcp_server.manager_digest)
    assert mcp_server.manager_escalations is not None
