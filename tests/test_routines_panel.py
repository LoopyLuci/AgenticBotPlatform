"""Routines as a screen and a CLI (roadmap P6): /api/routines, the Routines page in both UIs, and
`abp_cli routine ...`.

The API tests are round trips against the real FastAPI app with the real scheduler behind it - a
schedule created here is the row bot/scheduler.py's poll loop actually fires, and running the
routine goes through the real agent-loop engine. The one thing stubbed is the model behind
router.ask(), which is not what is under test; everything from the HTTP request down to the
routine_runs row is the production path. tests/conftest.py's temp_db keeps all of it in tmp_path.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances, db, routines, scheduler
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Dashboard-Token": "test-token"}
TEMPLATE = "Summarise the open pull requests in {{repo}} from the last {{days}} days and list anything blocked."
PARAMS = {"repo": {"description": "owner/name"}, "days": {"description": "how far back", "default": 7}}


@pytest.fixture
def iid(temp_db):
    return bot_instances.create_instance(name="r", platform="telegram", backend="api",
                                        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
                                        allowed_user_ids=[1])


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_routes_need_a_token(client):
    for method, path in (("get", "/api/routines"), ("get", "/api/routines/1"), ("get", "/api/routines/1/history"),
                         ("post", "/api/routines/1/run"), ("post", "/api/routines/1/pause"),
                         ("post", "/api/routines/1/resume"), ("put", "/api/routines/1/schedule"),
                         ("delete", "/api/routines/1")):
        assert client.request(method, path).status_code == 401, f"{method.upper()} {path} is open without a token"


def test_a_paired_phone_may_read_but_not_run_or_delete(client, iid):
    """Reading follows the dashboard's normal auth; running a routine is an agent turn and deleting
    one loses its history, so both are the dashboard token's alone."""
    _key_id, key = db.create_api_key("my-phone", permission_tier="standard")
    phone = {"X-Dashboard-Token": key}
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    assert client.get("/api/routines", headers=phone).status_code == 200
    assert client.get(f"/api/routines/{rid}", headers=phone).status_code == 200
    assert client.post(f"/api/routines/{rid}/pause", headers=phone).status_code == 401
    assert client.post(f"/api/routines/{rid}/run", json={"values": {"repo": "o/r"}}, headers=phone).status_code == 401
    assert client.delete(f"/api/routines/{rid}", headers=phone).status_code == 401
    assert routines.get_by_id(rid) is not None, "nothing was actually changed by the refused calls"


def test_the_list_shows_everything_a_table_needs(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, description="weekly", params=PARAMS)
    row = client.get("/api/routines", headers=H).json()["routines"][0]
    assert row["id"] == rid and row["name"] == "digest" and row["description"] == "weekly"
    assert list(row["params"]) == ["repo", "days"]
    assert row["last_run"] is None and row["next_run_at"] is None
    assert row["scheduled"] is False and row["paused"] is False and row["interval_s"] is None

    # Only this instance's routines when one is named.
    other = bot_instances.create_instance(name="other", platform="telegram", backend="api",
                                          credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
                                          allowed_user_ids=[2])
    routines.save(other, "theirs", "Do the other thing please, now.", params={})
    assert [r["name"] for r in client.get("/api/routines", headers=H).json()["routines"]] == ["digest", "theirs"]
    assert [r["name"] for r in client.get(f"/api/routines?instance_id={iid}", headers=H).json()["routines"]] == ["digest"]


def test_scheduling_through_the_api_is_a_row_the_real_scheduler_fires(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    r = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "1h", "values": {"repo": "o/r"},
                                                                    "chat_id": "5"})
    assert r.status_code == 200 and r.json()["interval_s"] == 3600
    row = db.get_scheduled_command(r.json()["schedules"][0]["id"])
    assert row["kind"] == "routine" and "o/r" in row["prompt"] and row["chat_id"] == "5"
    assert routines.routine_for_schedule(row["id"]) == rid

    listed = client.get("/api/routines", headers=H).json()["routines"][0]
    assert listed["scheduled"] and not listed["paused"] and listed["interval_s"] == 3600
    assert listed["next_run_at"] == row["next_run_at"]


def test_re_timing_re_renders_the_prompt_and_validates_before_saving(client, iid):
    """The point of checking first: a template that only breaks at 3 a.m. is caught while the person
    is still looking at the page, and the routine is left exactly as it was."""
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    sched_id = routines.schedules(rid)[0]["id"]
    before = db.get_scheduled_command(sched_id)

    bad = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "1h", "values": {}})
    assert bad.status_code == 400 and "needs a value for: repo" in bad.json()["detail"]
    worse = client.put(f"/api/routines/{rid}/schedule", headers=H,
                       json={"interval": "1h", "values": {"repo": "o/r", "colour": "red"}})
    assert worse.status_code == 400 and "no parameter colour" in worse.json()["detail"]
    too_short = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "1s", "values": {"repo": "o/r"}})
    assert too_short.status_code == 400 and "at least 5 seconds" in too_short.json()["detail"]
    nonsense = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "soon", "values": {"repo": "o/r"}})
    assert nonsense.status_code == 400 and "unrecognized interval" in nonsense.json()["detail"]
    after = db.get_scheduled_command(sched_id)
    assert (after["interval_s"], after["prompt"], after["next_run_at"]) == (before["interval_s"], before["prompt"], before["next_run_at"])

    ok = client.put(f"/api/routines/{rid}/schedule", headers=H,
                    json={"interval": "2h", "values": {"repo": "o/other", "days": 3}})
    assert ok.status_code == 200
    changed = db.get_scheduled_command(sched_id)
    assert changed["interval_s"] == 7200 and "o/other" in changed["prompt"] and "last 3 days" in changed["prompt"]
    assert changed["chat_id"] == "5", "an unstated chat is left alone"


def test_a_routine_with_no_schedule_needs_a_chat_before_it_can_be_timed(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    missing = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "1d", "values": {"repo": "o/r"}})
    assert missing.status_code == 400 and "no chat to deliver to" in missing.json()["detail"]
    assert client.get(f"/api/routines/{rid}", headers=H).json()["schedules"] == []
    made = client.put(f"/api/routines/{rid}/schedule", headers=H,
                      json={"interval": "1d", "values": {"repo": "o/r"}, "chat_id": "9"})
    assert made.status_code == 200 and made.json()["schedules"][0]["interval_s"] == 86400


def test_pausing_and_resuming_flips_every_schedule_and_shows_in_the_listing(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    r = routines.get_by_id(rid)
    first = routines.schedule(r, "5", 3600, {"repo": "o/r"})
    second = routines.schedule(r, "6", 7200, {"repo": "o/r"})

    assert client.post(f"/api/routines/{rid}/pause", headers=H).json() == {"paused": True, "changed": 2}
    assert db.get_scheduled_command(first)["enabled"] == 0 and db.get_scheduled_command(second)["enabled"] == 0
    listed = client.get("/api/routines", headers=H).json()["routines"][0]
    assert listed["paused"] and listed["next_run_at"] is None, "a paused routine has no next run to show"
    assert client.post(f"/api/routines/{rid}/resume", headers=H).json() == {"paused": False, "changed": 2}
    assert db.get_scheduled_command(first)["enabled"] == 1
    assert client.get("/api/routines", headers=H).json()["routines"][0]["next_run_at"] is not None


def test_run_now_goes_through_the_engine_and_lands_in_the_history(client, iid, monkeypatch):
    """The real run_turn (queueing, the backend call, on_result) with only the model behind
    router.ask() replaced - so this proves the API dispatches the engine's own scheduled path and
    records the outcome, not that a stub was called."""
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    sent = []

    async def fake_ask(prompt, **kwargs):
        sent.append((prompt, kwargs))
        from types import SimpleNamespace

        return SimpleNamespace(text="posted the summary")

    monkeypatch.setattr("bot.router.router.ask", fake_ask)
    monkeypatch.setattr("bot.outbox.send_message", _never_sends)

    started = client.post(f"/api/routines/{rid}/run", headers=H, json={"values": {"repo": "o/other", "days": 2}})
    assert started.status_code == 200 and started.json()["state"] == "background"
    assert started.json()["delivered_to"] == "5", "with no chat given it delivers where the routine already does"
    run = client.get(f"/api/routines/{rid}/history", headers=H).json()["history"][0]
    assert run["outcome"] == "ok" and "posted the summary" in run["summary"], "the run finished and wrote its own outcome"
    assert sent and "o/other" in sent[0][0] and "last 2 days" in sent[0][0]
    assert sent[0][1]["action_type"] == "scheduled" and sent[0][1]["instance_id"] == iid


def test_run_now_validates_the_parameters_before_promising_a_run(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    missing = client.post(f"/api/routines/{rid}/run", headers=H, json={"values": {}})
    assert missing.status_code == 400 and "needs a value for: repo" in missing.json()["detail"]
    unknown = client.post(f"/api/routines/{rid}/run", headers=H, json={"values": {"repo": "o/r", "colour": "red"}})
    assert unknown.status_code == 400 and "no parameter colour" in unknown.json()["detail"]
    assert routines.history(rid) == [], "a refused run leaves nothing behind"


def test_the_history_endpoint_and_the_detail_view(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    routines.record_run(rid, "ok", "the first one")
    routines.record_run(rid, "error", "the second one went wrong")
    detail = client.get(f"/api/routines/{rid}", headers=H).json()
    assert detail["template"] == TEMPLATE and list(detail["params"]) == ["repo", "days"]
    assert [h["outcome"] for h in detail["history"]] == ["error", "ok"]
    assert client.get(f"/api/routines/{rid}/history?limit=1", headers=H).json()["history"][0]["outcome"] == "error"
    assert client.get(f"/api/routines/{rid}/history?limit=0", headers=H).status_code == 422
    assert client.get("/api/routines/9999", headers=H).status_code == 404
    assert client.get("/api/routines/9999/history", headers=H).status_code == 404


def test_the_scheduler_poll_loop_actually_fires_a_routine_scheduled_from_the_api(client, iid, monkeypatch):
    """The end-to-end claim: a routine timed through /api/routines is fired by the real
    scheduler.run_forever poll loop, and the result shows up in the routine's own history."""
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    made = client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "5s", "values": {"repo": "o/r"},
                                                                       "chat_id": "5"})
    sched_id = made.json()["schedules"][0]["id"]
    fired = []

    async def fake_ask(prompt, **kwargs):
        fired.append(prompt)
        from types import SimpleNamespace

        return SimpleNamespace(text="done: 3 PRs")

    monkeypatch.setattr("bot.router.router.ask", fake_ask)
    monkeypatch.setattr("bot.outbox.send_message", _never_sends)
    # Make it due now rather than waiting out the interval.
    with db._lock:
        conn = db.get_conn()
        conn.execute("UPDATE scheduled_commands SET next_run_at=? WHERE id=?", (db._now(), sched_id))
        conn.commit()

    async def poll_once():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop))
        for _ in range(200):
            if routines.history(rid):
                break
            await asyncio.sleep(0.02)
        stop.set()
        await task

    asyncio.run(poll_once())
    assert fired and "o/r" in fired[0]
    run = routines.history(rid)[0]
    assert run["outcome"] == "ok" and "3 PRs" in run["summary"] and run["schedule_id"] == sched_id
    assert client.get(f"/api/routines/{rid}/history", headers=H).json()["history"][0]["outcome"] == "ok"


def test_delete_takes_the_schedules_and_the_history_with_it(client, iid):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    routines.record_run(rid, "ok", "once")
    gone = client.delete(f"/api/routines/{rid}", headers=H)
    assert gone.status_code == 200 and gone.json() == {"deleted": "digest", "schedules": 1}
    assert db.get_scheduled_command(sched_id) is None, "a deleted routine's schedule would otherwise keep firing"
    assert routines.history(rid) == [] and routines.get_by_id(rid) is None
    assert client.delete(f"/api/routines/{rid}", headers=H).status_code == 404
    assert client.post(f"/api/routines/{rid}/pause", headers=H).status_code == 404
    assert client.post(f"/api/routines/{rid}/resume", headers=H).status_code == 404
    assert client.put(f"/api/routines/{rid}/schedule", headers=H, json={"interval": "1h"}).status_code == 400


async def _never_sends(*a, **kw):
    """No live platform in a test: the turn is the point, not the delivery."""
    return None


# ---- the screen in both UIs -------------------------------------------------------------------------------------
def test_the_routines_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static/routines-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/routines-panel.js").read_text(encoding="utf-8")
    assert dash == desk, "the two UIs must not drift into showing different things"


def test_both_pages_mount_the_routines_screen_and_navigate_to_it():
    """An id in the HTML with no panel behind it is a screen that says "Connecting…" forever, and a
    screen nobody can reach is worse - the nav link is what makes it a page of its own."""
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="rt-root"' in text, f"{page} has no place to draw the screen"
        assert '<section id="routines">' in text, f"{page} has no Routines section"
        assert 'href="#routines"' in text, f"{page} cannot navigate to the Routines screen"
        assert re.search(r'<script src="(/static/)?routines-panel\.js"></script>', text), f"{page} never loads the panel"


def test_the_screen_drives_every_route_the_api_offers():
    """Each of the page's actions must name the route it calls, so a renamed route fails this test
    rather than leaving a button that quietly does nothing."""
    text = (ROOT / "bot/dashboard/static/routines-panel.js").read_text(encoding="utf-8")
    for needle in ("'/api/routines'", "/run`", "/pause`", "/resume`", "/schedule`", "method: 'DELETE'",
                   "data-rt-param", "parseInterval", "window.confirm"):
        assert needle in text, f"the panel never calls {needle}"
    assert "history" in text and "outcome" in text, "the detail view does not show the run history"


def test_the_dashboard_serves_the_panel(temp_db):
    assert TestClient(build_app()).get("/static/routines-panel.js").status_code == 200
