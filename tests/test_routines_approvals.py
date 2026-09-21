"""Routines (saved, parameterised, schedulable tasks) and approvals as reviewable objects - roadmap P6."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from bot import approvals_view, bot_instances, commands, db, routines, scheduler
from bot.agent_runtime import approval, tools
from bot.dashboard.server import build_app


@pytest.fixture
def iid(temp_db):
    return bot_instances.create_instance(name="w", platform="telegram", backend="api",
                                         credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])


def ctx(iid):
    return commands.CmdContext(instance_id=iid, instance_name="w", user_id=1, chat_id=5, actor="t")


def run(coro):
    return asyncio.run(coro)


TEMPLATE = "Summarise the open pull requests in {{repo}} from the last {{days}} days and list anything blocked."
PARAMS = {"repo": {"description": "owner/name"}, "days": {"description": "how far back", "default": 7}}


# ---- routines -------------------------------------------------------------------------------------------------
def test_a_routine_is_saved_rendered_and_listed(iid):
    rid = routines.save(iid, "Weekly-PRs", TEMPLATE, description="weekly summary", params=PARAMS)
    r = routines.get(iid, "weekly-prs")
    assert r["id"] == rid and r["params"]["days"]["default"] == 7
    assert routines.render(r, {"repo": "o/r"}) == "Summarise the open pull requests in o/r from the last 7 days and list anything blocked."
    assert "30 days" in routines.render(r, {"repo": "o/r", "days": 30})
    assert [x["name"] for x in routines.listing(iid)] == ["weekly-prs"]


def test_a_template_and_its_parameters_must_agree(iid):
    with pytest.raises(routines.RoutineError, match="not described"):
        routines.save(iid, "a", TEMPLATE, params={"repo": {"description": "x"}})
    with pytest.raises(routines.RoutineError, match="never uses"):
        routines.save(iid, "a", "Do the thing every day please.", params={"x": {"description": "unused"}})
    with pytest.raises(routines.RoutineError, match="lowercase"):
        routines.save(iid, "Bad Name!", TEMPLATE, params=PARAMS)
    with pytest.raises(routines.RoutineError, match="too short"):
        routines.save(iid, "a", "hi", params={})
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    with pytest.raises(routines.RoutineError, match="already exists"):
        routines.save(iid, "a", TEMPLATE, params=PARAMS)
    routines.save(iid, "a", TEMPLATE + " Be brief.", params=PARAMS, replace=True)
    assert routines.get(iid, "a")["template"].endswith("Be brief.")


def test_missing_and_unknown_values_are_caught_before_a_run(iid):
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    r = routines.get(iid, "a")
    with pytest.raises(routines.RoutineError, match="needs a value for: repo"):
        routines.render(r, {})
    with pytest.raises(routines.RoutineError, match="no parameter colour"):
        routines.render(r, {"repo": "o/r", "colour": "red"})


def test_values_are_plain_text_not_more_template(iid):
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    text = routines.render(routines.get(iid, "a"), {"repo": "{{days}}", "days": "3"})
    assert text.startswith("Summarise the open pull requests in {{days}} from the last 3 days"), "a value is never expanded again"


def test_scheduling_uses_the_scheduler_and_records_runs_and_can_be_paused(iid):
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    r = routines.get(iid, "a")
    sid = routines.schedule(r, chat_id=5, interval_s=3600, values={"repo": "o/r"})
    row = db.get_scheduled_command(sid)
    assert row["kind"] == "routine" and "o/r" in row["prompt"] and row["enabled"] == 1
    assert routines.routine_for_schedule(sid) == r["id"]
    routines.record_scheduled_run(sid, "ok", "posted the summary")
    routines.record_scheduled_run(sid, "error", "boom")
    assert [h["outcome"] for h in routines.history(r["id"])] == ["error", "ok"]
    assert routines.set_active(r["id"], False) == 1 and db.get_scheduled_command(sid)["enabled"] == 0
    assert routines.set_active(r["id"], True) == 1 and db.get_scheduled_command(sid)["enabled"] == 1
    listing = routines.listing(iid)[0]
    assert listing["schedules"][0]["id"] == sid and listing["last_run"]["outcome"] == "error"
    routines.delete(r["id"])
    assert db.get_scheduled_command(sid) is None and routines.get(iid, "a") is None and routines.history(r["id"]) == []


def test_scheduling_fails_now_when_a_value_is_missing_and_the_bad_interval_is_reported(iid):
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    r = routines.get(iid, "a")
    with pytest.raises(routines.RoutineError, match="needs a value"):
        routines.schedule(r, 5, 3600, {})
    with pytest.raises(routines.RoutineError, match="at least 5 seconds"):
        routines.schedule(r, 5, 1, {"repo": "o/r"})


def test_the_scheduler_records_a_routines_scheduled_run(iid, monkeypatch):
    routines.save(iid, "a", TEMPLATE, params=PARAMS)
    r = routines.get(iid, "a")
    sid = routines.schedule(r, 5, 3600, {"repo": "o/r"})
    row = db.get_scheduled_command(sid)

    async def fake_run_turn(prompt, *, on_result=None, **kw):
        assert "o/r" in prompt and kw["action_type"] == "scheduled"
        from types import SimpleNamespace

        await on_result("ran", SimpleNamespace(text="done: 3 PRs"))

    async def fake_send(*a, **kw):
        return None

    monkeypatch.setattr("bot.agent_runtime.engine.run_turn", fake_run_turn)
    monkeypatch.setattr(scheduler.outbox, "send_message", fake_send)
    run(scheduler._fire(row))
    assert routines.history(r["id"])[0]["outcome"] == "ok" and "3 PRs" in routines.history(r["id"])[0]["summary"]


def test_the_agent_saves_a_routine_with_a_tool(iid):
    out = run(tools.execute_tool("routine_save", {"name": "digest", "description": "d", "template": TEMPLATE, "params": PARAMS},
                                 workspace=None, instance_id=iid))
    assert "Saved routine 'digest' with 2 parameter(s)" in out and "/routine run digest repo=... days=..." in out
    with pytest.raises(Exception, match="already exists"):
        run(tools.execute_tool("routine_save", {"name": "digest", "template": TEMPLATE, "params": PARAMS}, workspace=None, instance_id=iid))
    assert tools.is_dangerous("routine_save"), "saving something that runs unattended later must be approved"


def test_the_slash_command_manages_routines(iid, monkeypatch):
    routines.save(iid, "digest", TEMPLATE, description="weekly summary", params=PARAMS)
    c = ctx(iid)
    assert "digest(repo, days): weekly summary - not scheduled" in run(commands.cmd_routine(c, "list"))
    assert "Template:" in run(commands.cmd_routine(c, "show digest")) and "how far back (default 7)" in run(commands.cmd_routine(c, "show digest"))
    assert "Scheduled digest as #" in run(commands.cmd_routine(c, "schedule digest every 7d repo=o/r"))
    assert "every 604800s on" in run(commands.cmd_routine(c, "list"))
    assert "Paused 1" in run(commands.cmd_routine(c, "pause digest")) and "Resumed 1" in run(commands.cmd_routine(c, "resume digest"))
    assert "needs a value for: repo" in run(commands.cmd_routine(c, "run digest"))
    sent = []

    async def fake_ask(cx, prompt):
        sent.append(prompt)
        return "asked"

    monkeypatch.setitem(commands._RAW_ARG_COMMANDS, "ask", fake_ask)
    assert run(commands.cmd_routine(c, "run digest repo=o/r days=2")) == "asked" and "o/r from the last 2 days" in sent[0]
    assert "run by hand" in run(commands.cmd_routine(c, "history digest"))
    assert "No routine named 'nope'" in run(commands.cmd_routine(c, "run nope"))
    assert "Usage: /routine schedule" in run(commands.cmd_routine(c, "schedule digest"))
    assert "Deleted digest" in run(commands.cmd_routine(c, "delete digest")) and "No routines yet" in run(commands.cmd_routine(c, "list"))


# ---- approvals ----------------------------------------------------------------------------------------------------
def test_an_edit_approval_shows_the_change_as_a_diff():
    p = approvals_view.preview("edit_file", {"path": "app.py", "old_string": "return a - b", "new_string": "return a + b"})
    assert p["kind"] == "diff" and p["summary"] == "Change app.py" and "-return a - b" in p["body"] and "+return a + b" in p["body"]
    assert "new file" in approvals_view.preview("edit_file", {"path": "n.py", "old_string": "", "new_string": "x = 1"})["body"]
    m = approvals_view.preview("multi_edit", {"path": "a.py", "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]})
    assert m["summary"] == "Make 2 edits to a.py" and m["body"].count("@@") >= 2


def test_other_tools_are_described_plainly():
    assert approvals_view.preview("run_shell", {"command": "rm -rf build"}) == {"summary": "Run a command", "kind": "command", "body": "rm -rf build"}
    w = approvals_view.preview("write_file", {"path": "x.txt", "content": "hello"})
    assert "Write x.txt (5 characters" in w["summary"] and w["body"] == "hello"
    patch = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-1\n+2\n"
    assert approvals_view.preview("apply_patch", {"patch": patch})["summary"] == "Apply a patch to f.py"
    b = approvals_view.preview("browser_act", {"action": "type", "ref": 3, "text": "my private note"})
    assert "my private note" not in b["body"] and "(typed text)" in b["body"]
    assert "do something" in approvals_view.preview("browser_handoff", {"reason": "solve it"})["summary"]
    assert approvals_view.preview("mystery", {"a": 1})["summary"] == "Use mystery"
    assert "more characters" in approvals_view.preview("write_file", {"path": "big", "content": "x" * 9000})["body"]


def test_secrets_never_appear_in_a_reviewable_approval(iid, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "server-secret-value-123")
    aid = db.create_pending_approval(iid, 5, "sess", "run_shell", {"command": "curl -H 'k: server-secret-value-123' x"})
    assert "server-secret-value-123" not in json.dumps(approvals_view.get(aid)) and "[secret:MY_API_KEY]" in approvals_view.get(aid)["body"]


def test_a_person_decides_from_anywhere_and_the_waiting_run_continues(iid):
    seen = {}

    async def scenario():
        async def notify(approval_id, tool, tool_input):
            seen["id"] = approval_id

        waiting = asyncio.create_task(approval.request_approval(iid, 5, "sess-x", "edit_file", {"path": "a.py", "old_string": "a", "new_string": "b"}, notify, timeout_s=10))
        for _ in range(100):
            if "id" in seen:
                break
            await asyncio.sleep(0.05)
        assert approvals_view.listing(instance_id=iid)[0]["id"] == seen["id"]
        assert approvals_view.decide(seen["id"], "once", "phone") == "decided"
        assert approvals_view.decide(seen["id"], "once", "phone") == "already_resolved"
        return await waiting

    assert run(scenario()) == "once"
    row = approvals_view.get(seen["id"])
    assert row["status"] == "approved_once" and row["resolved_by"] == "phone"
    assert approvals_view.listing(instance_id=iid) == [] and len(approvals_view.listing(status="all", instance_id=iid)) == 1
    assert approvals_view.decide(9999, "once", "x") == "not_found"
    with pytest.raises(ValueError):
        approvals_view.decide(seen["id"], "maybe", "x")


def test_a_phone_is_pushed_a_short_summary_when_an_approval_is_created(iid, monkeypatch):
    sent = []

    async def fake_push(name, summary, approval_id):
        sent.append((name, summary, approval_id))

    monkeypatch.setattr("bot.push.notify_approval", fake_push)

    async def scenario():
        async def notify(*a):
            return None

        task = asyncio.create_task(approval.request_approval(iid, 5, "sess-y", "run_shell", {"command": "rm -rf x"}, notify, timeout_s=0.3))
        return await task

    assert run(scenario()) == "deny"          # nobody answered
    assert sent and sent[0][0] == "w" and sent[0][1] == "Run a command" and "rm -rf" not in sent[0][1]


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


H = {"X-Dashboard-Token": "test-token"}


def test_the_approvals_api_lists_shows_and_reports_stale_answers(client, iid):
    aid = db.create_pending_approval(iid, 5, "sess", "edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"})
    assert client.get("/api/approvals").status_code == 401
    listed = client.get("/api/approvals", headers=H).json()["approvals"]
    assert listed[0]["id"] == aid and listed[0]["kind"] == "diff" and "+y" in listed[0]["body"]
    assert client.get(f"/api/approvals/{aid}", headers=H).json()["summary"] == "Change a.py"
    assert client.get("/api/approvals/999", headers=H).status_code == 404
    assert client.get("/api/approvals?status=bogus", headers=H).status_code == 422
    assert client.post(f"/api/approvals/{aid}/resolve", json={"outcome": "once"}, headers=H).status_code == 409, "no run is waiting on this row"
    assert client.post(f"/api/approvals/{aid}/resolve", json={"outcome": "maybe"}, headers=H).status_code == 400
    assert client.post("/api/approvals/999/resolve", json={"outcome": "once"}, headers=H).status_code == 404


def test_a_paired_phone_may_approve_once_but_not_grant_standing_approval(client, iid):
    _key_id, key = db.create_api_key("my-phone", permission_tier="standard")
    phone = {"X-Dashboard-Token": key}
    aid = db.create_pending_approval(iid, 5, "sess", "run_shell", {"command": "ls"})
    assert client.get("/api/approvals", headers=phone).status_code == 200, "a phone can see what is waiting"
    for outcome in ("always", "session"):
        r = client.post(f"/api/approvals/{aid}/resolve", json={"outcome": outcome}, headers=phone)
        assert r.status_code == 403 and "standing" in r.json()["detail"]
    assert client.post(f"/api/approvals/{aid}/resolve", json={"outcome": "once"}, headers=phone).status_code == 409    # allowed; just nothing waiting
    assert client.post(f"/api/approvals/{aid}/resolve", json={"outcome": "always"}, headers=H).status_code == 409     # the owner may
