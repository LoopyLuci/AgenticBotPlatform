"""LF3 phase 2: the recovery probe, automatic handoff packs, work queues that follow the capacity state, the capacity-change feed,
and the takeover drill (a multi-task run whose cloud provider runs out in the middle, with fakes only)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time

import pytest

from bot import capacity, coordination
from bot.capacity import autopack, events, follow, governor, handoff, probe
from bot.capacity.classify import Signal, USAGE_CAP
from bot.capacity.probe import Outcome
from bot.coordination import LocalBoard

NOW = 1_800_000_000.0
CREDIT = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                     "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}})


def cap(reset=None, retry=None):
    return Signal(USAGE_CAP, reset_at=reset, retry_after_s=retry, reason="usage limit reached")


@pytest.fixture(autouse=True)
def clean():
    governor._last_seen.clear()
    probe.set_prober(None)
    yield
    governor._last_seen.clear()
    probe.set_prober(None)


def capped(now=NOW, **kw):
    governor.observe(cap(**kw), provider="anthropic", model="m1", now=now)


def run(coro):
    return asyncio.run(coro)


# ---- the probe's schedule ----------------------------------------------------------------------------------------------
def test_nothing_is_probed_while_cloud_led_or_before_the_first_interval():
    assert governor.probe_claim(now=NOW) is None
    capped()
    assert governor.get_state(NOW)["state"] == "local-only"
    assert governor.probe_claim(now=NOW + 60) is None, "the first probe is one interval (300 s) after the cap"
    got = governor.probe_claim(now=NOW + 301)
    assert got["targets"] == [{"provider": "anthropic", "model": "m1"}] and got["timeout_s"] == 20.0
    assert governor.probe_claim(now=NOW + 302) is None, "a round is reserved once: a second loop does not probe again"


def test_a_working_probe_goes_to_hybrid_probation_and_a_second_one_returns_to_cloud_led():
    capped()
    assert governor.probe_claim(now=NOW + 301)
    v = governor.probe_finish(True, provider="anthropic", model="m1", status="ok", now=NOW + 302)
    assert v["state"] == "hybrid" and v["auto"]["probation"] and v["probe"]["last"]["ok"]
    assert governor.probe_claim(now=NOW + 302 + 100) is None, "not again straight away"
    assert governor.probe_claim(now=NOW + 302 + 301)
    v = governor.probe_finish(True, provider="anthropic", model="m1", now=NOW + 700)
    assert v["state"] == "cloud-led" and v["auto"]["strikes"] == 0 and v["probe"]["targets"] == []
    assert [h["kind"] for h in governor.history(10)][:2] == ["recovered", "probe_ok"]


def test_a_probe_does_not_override_the_owner_but_recovers_what_is_underneath():
    capped()
    governor.set_override("local-only", now=NOW + 1)
    assert governor.probe_claim(now=NOW + 301)
    v = governor.probe_finish(True, provider="anthropic", model="m1", now=NOW + 302)
    assert v["state"] == "local-only" and v["auto"]["state"] == "hybrid", "the pin stays; the automatic state underneath moved"


def test_failed_probes_back_off_and_the_hourly_limit_holds(monkeypatch):
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"probe": {"interval_s": 60, "max_interval_s": 400, "backoff": 2, "max_per_hour": 4}})
    capped()
    t, gaps = NOW, []
    for _ in range(6):
        t += 1
        nxt = governor.get_state(t)["probe"]["next_at"]
        t = max(t, nxt)
        if not governor.probe_claim(now=t):
            break
        governor.probe_finish(False, provider="anthropic", model="m1", status="error", detail="503", now=t)
        gaps.append(round(governor.get_state(t)["probe"]["next_at"] - t))
    assert gaps[:4] == [120, 240, 400, 400], "60 s doubling after each failure, never above max_interval_s"
    assert len(gaps) == 4, "four probes in the hour, then no more until the oldest ages out"
    s = governor.get_state(t)
    assert s["probe"]["last_hour"] == 4 and s["probe"]["fails"] == 4 and s["state"] == "local-only"
    assert s["probe"]["next_at"] >= NOW + 3600 - 5 or governor.probe_claim(now=t + 10) is None


def test_a_stated_reset_is_waited_for_and_a_later_one_in_the_answer_extends_the_hold():
    capped(reset=NOW + 4000)
    assert governor.probe_claim(now=NOW + 301) is None, "nothing before half of the stated hold has passed"
    assert governor.probe_claim(now=NOW + 2001)
    v = governor.probe_finish(False, provider="anthropic", model="m1", status="usage_cap", detail="still capped",
                              reset_at=NOW + 9000, now=NOW + 2002)
    assert v["state"] == "local-only" and v["auto"]["until"] == NOW + 9000 and v["auto"]["reset_known"]
    assert v["probe"]["next_at"] >= NOW + 9000, "a stated reset time is honoured by the next probe"


def test_a_retry_after_in_the_answer_sets_the_next_probe_time():
    capped()
    governor.probe_claim(now=NOW + 301)
    v = governor.probe_finish(False, provider="anthropic", model="m1", status="rate_limit", retry_after_s=1500, now=NOW + 302)
    assert v["probe"]["next_at"] == pytest.approx(NOW + 302 + 1500)


def test_force_skips_the_interval_and_a_cap_without_a_known_provider_cannot_be_probed(monkeypatch):
    governor.observe(cap(), provider="", model="", now=NOW)              # nothing says which provider
    assert governor.get_state(NOW)["state"] == "local-only"
    assert governor.probe_claim(force=True, now=NOW + 1) is None
    last = governor.get_state(NOW + 1)["probe"]["last"]
    assert last["status"] == "unprobeable" and "reset time" in last["detail"]
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"probe": {"targets": ["openrouter/free-model"]}})
    got = governor.probe_claim(force=True, now=NOW + 2)
    assert got["targets"] == [{"provider": "openrouter", "model": "free-model"}]


def test_the_probe_can_be_switched_off(monkeypatch):
    from bot.config import config

    capped()
    monkeypatch.setitem(config._data, "capacity", {"probe": {"enabled": False}})
    assert governor.probe_claim(force=True, now=NOW + 400) is None
    assert governor.get_state(NOW + 400)["probe"]["enabled"] is False


def test_the_schedule_survives_a_restart():
    capped()
    governor.probe_claim(now=NOW + 301)
    governor._last_seen.clear()
    s = governor.get_state(NOW + 310)
    assert s["probe"]["last_hour"] == 1 and s["probe"]["next_at"] > NOW + 310 and s["probe"]["targets"] == [{"provider": "anthropic", "model": "m1"}]


# ---- sending the probe ------------------------------------------------------------------------------------------------
def test_probe_once_sends_through_the_prober_and_applies_the_answer():
    capped(now=time.time() - 400)
    seen = []

    async def prober(provider, model, timeout_s):
        seen.append((provider, model, timeout_s, capacity.probing.get()))
        return Outcome(True, "ok")

    probe.set_prober(prober)
    res = run(probe.probe_once())
    assert res["ran"] and res["outcomes"] == [{"provider": "anthropic", "model": "m1", "ok": True, "status": "ok"}]
    assert seen == [("anthropic", "m1", 20.0, True)], "the sender runs with the probing flag set"
    assert res["state"]["state"] == "hybrid" and capacity.probing.get() is False
    assert run(probe.probe_once())["ran"] is False, "not due again yet"


def test_a_prober_that_raises_is_a_failed_probe_not_a_crash():
    capped(now=time.time() - 400)

    async def prober(provider, model, timeout_s):
        raise RuntimeError("dns down")

    probe.set_prober(prober)
    res = run(probe.probe_once())
    assert res["outcomes"][0]["ok"] is False and res["state"]["state"] == "local-only"
    assert res["state"]["probe"]["last"]["status"] == "error" and res["state"]["probe"]["fails"] == 1


def test_the_default_prober_sends_one_tiny_request_and_a_failure_is_not_counted_twice(monkeypatch):
    """Through a real transport class (so usage_limits wraps send): one user message, no tools, max_tokens 1; the call it makes
    is not reported to the governor again (no extra strike, no early recovery)."""
    from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport
    from bot.backends.base import BackendError

    sent = []
    outcome = {"fail": True}

    class T(ProviderTransport):
        base_url = "https://api.example-cloud.test/v1"
        catalog_id = "example-cloud"

        def user_message(self, text, *, images=None, documents=None):
            return {"role": "user", "content": text}

        async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
            sent.append(dict(model=model, history=history, tools=tool_schemas, max_tokens=max_tokens, system=system_prompt))
            if outcome["fail"]:
                self.last_status = 400
                raise BackendError(CREDIT)
            return NormalizedResponse(text="p", assistant_message={"role": "assistant", "content": "p"}, tokens=2)

    import abp_run.core as core

    monkeypatch.setattr(core, "transport_for", lambda provider, model: T())
    capped(now=time.time() - 400)
    strikes = governor.get_state()["auto"]["strikes"]
    res = run(probe.probe_once())
    assert sent == [dict(model="m1", history=[{"role": "user", "content": "ping"}], tools=[], max_tokens=1, system="")]
    assert res["outcomes"][0]["status"] == "usage_cap" and res["state"]["state"] == "local-only"
    assert res["state"]["auto"]["strikes"] == strikes, "the probe's own failure is not a new strike"
    outcome["fail"] = False
    res = run(probe.probe_once(force=True))
    assert res["outcomes"][0]["ok"] and res["state"]["state"] == "hybrid"


def test_unprobeable_targets_are_reported_not_guessed():
    out = run(probe.default_prober("", "", 5))
    assert not out.ok and out.status == "unprobeable"
    out = run(probe.default_prober("no-such-provider-xyz", "m", 5))
    assert not out.ok and out.status == "unprobeable"


# ---- the capacity-change feed (the LF5 hook) --------------------------------------------------------------------------------
def test_state_changes_and_probe_outcomes_are_published_after_they_commit(tmp_path):
    board = LocalBoard(tmp_path / "ev.db")
    coordination.set_board(board)
    heard, on_board = [], []
    off = events.subscribe(heard.append)
    board.listeners.append(on_board.append)
    since = (events.recent(0, 1) or [{"seq": 0}])[-1]["seq"]
    try:
        capped()
        governor.probe_claim(now=NOW + 301)
        governor.probe_finish(False, provider="anthropic", model="m1", status="error", detail="503", now=NOW + 302)
        governor.probe_claim(now=NOW + 302 + 130)
        governor.probe_finish(True, provider="anthropic", model="m1", now=NOW + 440)
        governor.probe_claim(now=NOW + 440 + 301)
        governor.probe_finish(True, provider="anthropic", model="m1", now=NOW + 800)
    finally:
        off()
        coordination.set_board(None)
        board.close()
    got = events.recent(since)
    assert [e["kind"] for e in got] == ["capacity.changed", "capacity.probe", "capacity.changed", "capacity.recovered"]
    assert got[0]["from"] == "cloud-led" and got[0]["to"] == "local-only" and "anthropic/m1" in got[0]["reason"]
    assert got[-1]["to"] == "cloud-led" and got[-1]["actor"] == "probe"
    assert [e["kind"] for e in heard] == [e["kind"] for e in got]
    assert [e["kind"] for e in on_board] == [e["kind"] for e in got], "the same events are on the local work board's feed"
    lines = events.digest_lines(since)
    assert len(lines) == 4 and "capacity.recovered" in lines[-1] and "hybrid -> cloud-led" in lines[-1]


def test_a_listener_that_raises_does_not_break_the_governor():
    def bad(_e):
        raise RuntimeError("boom")

    off = events.subscribe(bad)
    try:
        capped()
        assert governor.get_state(NOW)["state"] == "local-only"
    finally:
        off()


# ---- following the state: the capacity hint on board tasks ---------------------------------------------------------------
def test_hints_become_needs_and_workers_advertise_what_the_state_allows():
    assert follow.need_for("cloud") == ["capacity:cloud"] and follow.need_for("") == [] and follow.need_for("capacity:local") == ["capacity:local"]
    with pytest.raises(ValueError):
        follow.need_for("gpu")
    assert follow.tag(["run:x", "capacity:local"], "cloud") == ["run:x", "capacity:cloud"]
    assert follow.hint_of({"needs": ["a", "capacity:cloud"]}) == "cloud" and follow.hint_of({"needs": []}) == ""
    assert follow.worker_capabilities(["instance:1"], state="cloud-led") == ["instance:1", "capacity:local", "capacity:cloud"]
    assert follow.worker_capabilities(["instance:1"], state="hybrid") == ["instance:1", "capacity:local", "capacity:cloud"]
    assert follow.worker_capabilities(["instance:1"], state="local-only") == ["instance:1", "capacity:local"]


def test_placement_follows_the_hint_then_the_state(monkeypatch):
    monkeypatch.setattr("bot.capacity.routing.local_candidates", lambda: ["modelmistress/qwen"])
    assert follow.placement({"needs": []}, state="cloud-led")["where"] == "any"
    p = follow.placement({"needs": []}, state="local-only")
    assert p["where"] == "local" and p["models"] == ["modelmistress/qwen"]
    assert follow.placement({"needs": ["capacity:local"]}, state="cloud-led")["where"] == "local"
    assert follow.placement({"needs": ["capacity:cloud"]}, state="hybrid")["where"] == "cloud"
    assert follow.placement({"needs": ["capacity:cloud"]}, state="local-only")["where"] == "local", "never a cloud model in local-only"
    assert follow.placement({"title": "what is 2 + 2?"}, state="hybrid")["where"] == "local"
    assert follow.placement({"title": "analyze the trade-offs of this architecture and design a migration strategy"}, state="hybrid")["where"] == "cloud"


def test_the_board_hands_a_worker_only_what_its_capabilities_allow(tmp_path):
    board = LocalBoard(tmp_path / "q.db")
    try:
        easy, _ = board.create_task("conductor", "easy", key="e", needs=follow.need_for(""))
        hard, _ = board.create_task("conductor", "hard", key="h", needs=follow.need_for("cloud"))
        governor.set_override("local-only", now=time.time())
        got = board.next("conductor", "w1", capabilities=follow.worker_capabilities(["x"]), lease_s=30)
        assert got["task"]["id"] == easy["id"]
        board.set_state(easy["id"], "w1", got["fence"], "done")
        assert board.next("conductor", "w1", capabilities=follow.worker_capabilities(["x"]), lease_s=30) is None, \
            "the cloud-marked task waits while capacity is local-only (it is not failed or dropped)"
        assert board.get_task(hard["id"])["state"] == "open"
        governor.clear_override()
        got = board.next("conductor", "w1", capabilities=follow.worker_capabilities(["x"]), lease_s=30)
        assert got["task"]["id"] == hard["id"]
    finally:
        board.close()


# ---- swarm steps follow the state ---------------------------------------------------------------------------------------
def _swarm(call, board):
    from bot.swarm.board_run import SwarmBoard

    sb = SwarmBoard({"id": 7}, "the prompt", "run1", call)
    sb.board = board
    return sb


def _step(i, **kw):
    from bot.swarm.board_run import Step

    return Step(id=f"s{i}", instance_id=1, role="worker", label=f"bot{i}", prompt=lambda _o, _i=i: f"do {_i}", **kw)


def test_a_cloud_marked_step_waits_for_capacity_and_nothing_else_does(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.swarm.board_run.POLL_S", 0.02)
    board = LocalBoard(tmp_path / "s.db")
    ran = []

    async def call(instance_id, text):
        ran.append(text)
        return "ok " + text

    async def scenario():
        governor.set_override("local-only", now=time.time())
        sb = _swarm(call, board)
        task = asyncio.ensure_future(sb.run([_step(1), _step(2, capacity="cloud"), _step(3)]))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(ran) == 2:
                break
        assert sorted(ran) == ["do 1", "do 3"] and not task.done(), "the unmarked steps ran; the cloud-marked one waits"
        governor.clear_override()
        return await asyncio.wait_for(task, 10)

    try:
        out = run(scenario())
    finally:
        board.close()
    assert sorted(ran) == ["do 1", "do 2", "do 3"] and {o["status"] for o in out.values()} == {"success"}


def test_a_step_whose_cloud_call_ran_out_is_handed_back_and_runs_again(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.swarm.board_run.POLL_S", 0.02)
    board = LocalBoard(tmp_path / "h.db")
    tries = {"s1": 0, "s2": 0}

    async def call(instance_id, text):
        key = "s1" if text == "do 1" else "s2"
        tries[key] += 1
        if key == "s1" and tries[key] == 1:
            raise RuntimeError("Your credit balance is too low to access the Anthropic API")
        if key == "s2":
            raise RuntimeError("syntax error in the model's output")        # not capacity: fails as before
        return "done on the local model"

    try:
        out = run(_swarm(call, board).run([_step(1), _step(2, )]))
        states = {t["title"]: t["state"] for t in board.list_tasks("abp/swarm-7")}
    finally:
        board.close()
    assert tries == {"s1": 2, "s2": 1}
    assert out["s1"]["status"] == "success" and out["s2"]["status"] == "failed"
    assert sorted(states.values()) == ["done", "failed"]


def test_a_capacity_error_is_handed_back_a_bounded_number_of_times(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.swarm.board_run.POLL_S", 0.02)
    board = LocalBoard(tmp_path / "b.db")
    n = {"calls": 0}

    async def call(instance_id, text):
        n["calls"] += 1
        raise RuntimeError("usage limit reached")

    try:
        out = run(_swarm(call, board).run([_step(1)]))
    finally:
        board.close()
    assert n["calls"] == follow.MAX_REQUEUES + 1 and out["s1"]["status"] == "failed"


# ---- automatic handoff packs ----------------------------------------------------------------------------------------------
class TC:
    def __init__(self, name, **arguments):
        self.name, self.arguments, self.id = name, arguments, name


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(2000)), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "task", str(wt))
    return wt


def test_a_tool_round_updates_the_pack_files_tests_and_errors(tmp_path):
    rec = autopack.begin("fix the parser", model="cloud/m1", workspace=tmp_path, context={"handoff_task_id": "t-rounds"})
    assert rec and handoff.read("t-rounds").goal == "fix the parser" and handoff.read("t-rounds").model == "cloud/m1"

    async def go():
        await rec.after_round([(TC("edit_file", path="src/p.py", old_string="a", new_string="b"), "Edited src/p.py")])
        assert rec.writes == 0, "a plain file edit is throttled (min_interval_s), not written every round"
        await rec.after_round([(TC("run_tests", paths=["tests/test_p.py"]), "1 failed, 4 passed in 0.5s\nFAILED tests/test_p.py::test_x")])
        await rec.after_round([(TC("read_file", path="nope"), "Error: no such file: nope")])
        await rec.finish(False, error="BackendError: usage limit", interrupted=True)

    run(go())
    p = handoff.read("t-rounds")
    assert p.files_touched == ["src/p.py"] and p.status == "interrupted"
    assert p.test_status["status"] == "failing" and "1 failed" in p.test_status["summary"] and p.test_status["command"].startswith("run_tests")
    assert p.last_error.startswith("BackendError") and rec.writes >= 2


def test_a_shell_test_command_counts_and_other_shell_output_does_not(tmp_path):
    rec = autopack.begin("p", model="m", workspace=tmp_path, context={"handoff_task_id": "t-shell"})

    async def go():
        await rec.after_round([(TC("run_shell", command="ls"), "5 passed")])
        assert handoff.read("t-shell").test_status["status"] == "unknown"
        await rec.after_round([(TC("run_shell", command="python -m pytest -q"), "..\n12 passed in 1.1s")])

    run(go())
    assert handoff.read("t-shell").test_status["status"] == "passing"


def test_the_pack_of_a_task_with_a_worktree_has_its_diff_and_a_big_diff_is_summarised(worktree, monkeypatch):
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"handoff": {"max_diff_chars": 600}})
    rec = autopack.begin("change a", model="m", workspace=worktree, context={"handoff_task_id": "t-git"})
    (worktree / "a.py").write_text("x = 2\n", encoding="utf-8")
    (worktree / "new.py").write_text("y = 1\n", encoding="utf-8")
    run(rec.finish(True, "done"))
    p = handoff.read("t-git")
    assert "-x = 1" in p.diff and "+x = 2" in p.diff and "new.py" in p.diff
    assert sorted(p.files_touched) == ["a.py", "new.py"] and p.status == "finished" and p.capacity == "cloud-led"
    (worktree / "big.txt").write_text("\n".join(f"changed {i}" for i in range(2000)), encoding="utf-8")
    rec2 = autopack.begin("again", model="m", workspace=worktree, context={"handoff_task_id": "t-git2"})
    run(rec2.finish(True))
    d = handoff.read("t-git2").diff
    assert len(d) <= 600 and "summary" in d and "big.txt" in d and "changed 1999" not in d, "above the limit: the --stat summary, not the diff"


def test_a_plain_folder_is_not_diffed(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    rec = autopack.begin("p", model="m", workspace=tmp_path, context={"handoff_task_id": "t-plain"})
    run(rec.finish(True))
    assert handoff.read("t-plain").diff == ""


def test_another_model_taking_over_starts_from_the_pack(tmp_path):
    first = autopack.begin("port the parser", model="anthropic/big", workspace=tmp_path, context={"handoff_task_id": "t-take"})
    first.note("the tokenizer must stay lazy")
    run(first.after_round([(TC("write_file", path="p.py", content="x"), "Wrote p.py")]))
    run(first.finish(False, error="usage limit reached", interrupted=True))
    again = autopack.begin("port the parser", model="anthropic/big", workspace=tmp_path, context={"handoff_task_id": "t-take"})
    assert again.resume_text == "", "the same model carries on from its own conversation"
    run(again.finish(False, error="usage limit reached", interrupted=True))
    second = autopack.begin("port the parser", model="modelmistress/qwen", workspace=tmp_path, context={"handoff_task_id": "t-take"})
    t = second.resume_text
    assert "taking over task t-take from anthropic/big" in t and "GOAL: port the parser" in t and "p.py" in t and "tokenizer must stay lazy" in t
    assert "usage limit reached" in t
    assert any("resumed on modelmistress/qwen" in n for n in handoff.read("t-take").notes)
    run(second.finish(True, "all done"))
    third = autopack.begin("port the parser", model="other/model", workspace=tmp_path, context={"handoff_task_id": "t-take"})
    assert third.resume_text == "", "a finished task is not resumed"


def test_ordinary_chat_and_disabled_packs_cost_nothing(tmp_path, monkeypatch):
    from bot.config import config

    assert autopack.begin("hi", model="m", workspace=tmp_path, context={}) is None
    monkeypatch.setitem(config._data, "capacity", {"handoff": {"auto": False}})
    assert autopack.begin("hi", model="m", workspace=tmp_path, context={"handoff_task_id": "x"}) is None
    assert handoff.read("x") is None


def test_a_turn_inside_another_turn_leaves_the_parents_pack_alone(tmp_path):
    outer = autopack.begin("outer", model="m", workspace=tmp_path, context={"handoff_task_id": "t-out"})
    token = autopack.active.set(outer)
    try:
        assert autopack.begin("inner", model="other", workspace=tmp_path, context={"handoff_task_id": ""}) is None
        mine = autopack.begin("inner", model="other", workspace=tmp_path, context={"handoff_task_id": "t-inner"})
        assert mine is not None and handoff.read("t-inner").model == "other", "unless its caller names a task of its own"
    finally:
        autopack.active.reset(token)


def test_a_swarm_step_gets_a_pack_named_by_its_board_task(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.swarm.board_run.POLL_S", 0.02)
    board = LocalBoard(tmp_path / "p.db")
    made = {}

    async def call(instance_id, text):
        rec = autopack.begin(text, model="cloud/m", workspace=tmp_path, context={})
        made[text] = rec.task_id
        rec.note("working")
        await rec.finish(False, error="usage limit", interrupted=True)
        raise RuntimeError("usage limit reached")

    try:
        out = run(_swarm(call, board).run([_step(1)]))
        task = board.list_tasks("abp/swarm-7")[0]
    finally:
        board.close()
    assert made["do 1"] == task["id"], "inside a board task the pack is named after the task"
    pack = handoff.read(task["id"])
    assert pack.goal.startswith("worker: bot1") and pack.board == "abp/swarm-7" and out["s1"]["status"] == "failed"


# ---- the native agent loop, end to end ------------------------------------------------------------------------------------
def _native_turn(tmp_path, model, script, task_id, *, fail_after=None):
    from abp_agenteval.scripted import ScriptedTransport
    from bot.backends.base import BackendError
    from bot.backends.native_backend import NativeAgentBackend

    prompts = []

    class T(ScriptedTransport):
        async def send(self, **kw):
            prompts.append(kw["history"][0]["content"] if kw["history"] else "")
            if fail_after is not None and self.calls >= fail_after:
                raise BackendError("usage limit reached")
            return await super().send(**kw)

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)

    async def turn():
        backend = NativeAgentBackend(T(script), model=model, name="t")
        return await backend.ask("write the parser", context={"cwd": str(ws), "handoff_task_id": task_id}, timeout_s=30)

    return turn, prompts, ws


def test_a_native_turn_writes_its_pack_as_it_goes_and_the_next_model_resumes_from_it(tmp_path):
    from abp_agenteval.runner import isolated_environment
    from abp_agenteval.task import Call, Say
    from bot.backends.base import BackendError

    with isolated_environment(tmp_path / "root", {}):
        turn, prompts, ws = _native_turn(tmp_path, "cloud-big",
                                         [Call("write_file", {"path": "parser.py", "content": "def parse(): ...\n"}), Say("never reached")],
                                         "t-native", fail_after=1)
        with pytest.raises(BackendError):
            run(turn())
        pack = handoff.read("t-native")
        assert pack.status == "failed" and "parser.py" in pack.files_touched and "usage limit" in pack.last_error
        assert pack.goal == "write the parser" and pack.model.endswith("/cloud-big")
        assert (ws / "parser.py").exists() and prompts[0] == "write the parser", "the first model gets the plain prompt"

        turn2, prompts2, _ = _native_turn(tmp_path, "local-qwen", [Say("continued")], "t-native")
        res = run(turn2())
        assert res.text == "continued"
        first = prompts2[0]
        assert first.startswith("You are taking over task t-native") and "parser.py" in first and "usage limit reached" in first
        assert first.endswith("write the parser"), "the resume text comes in front of the task's own prompt"
        done = handoff.read("t-native")
        assert done.status == "finished" and done.model.endswith("/local-qwen")
        assert any("resumed on" in n for n in done.notes)


# ---- the takeover drill -------------------------------------------------------------------------------------------------
def test_takeover_drill_cloud_runs_out_mid_run_every_task_finishes_locally_then_the_probe_brings_the_cloud_back(tmp_path, monkeypatch):
    """A 6-step run. After two steps the cloud starts answering usage-limit errors (and one step is in flight on the cloud when it
    happens). Every step still ends `done`, on the cloud before the cap, on a local model after it; the in-flight step is not
    interrupted; packs are written for every task; then the probe finds the cloud back and the state returns to cloud-led."""
    monkeypatch.setattr("bot.swarm.board_run.POLL_S", 0.01)
    board = LocalBoard(tmp_path / "drill.db")
    ran_on: dict[str, list[str]] = {}
    cloud_alive = {"ok": True}
    in_flight_started, release_in_flight = asyncio.Event(), asyncio.Event()
    since = (events.recent(0, 1) or [{"seq": 0}])[-1]["seq"]

    async def call(instance_id, text):
        step = text.split()[-1]
        where = "local" if governor.effective_state() == "local-only" else "cloud"
        rec = autopack.begin(text, model=f"{where}/m", workspace=tmp_path, context={})
        ran_on.setdefault(step, []).append(where)
        if rec.resume_text:
            ran_on[step].append("resumed-from-pack")
        if where == "cloud":
            if step == "5":                                   # in flight when the cap hits: it must run to the end
                in_flight_started.set()
                await release_in_flight.wait()
            elif not cloud_alive["ok"]:
                capacity.observe_failure("anthropic", "m", 400, CREDIT)
                await rec.finish(False, error="credit balance is too low", interrupted=True)
                raise RuntimeError("Your credit balance is too low to access the Anthropic API")
            elif step == "3":
                await asyncio.wait_for(in_flight_started.wait(), 10)        # step 5 is on the cloud before the cap lands
                await rec.after_round([(TC("write_file", path="s3.py", content="x"), "Wrote s3.py")])
                cloud_alive["ok"] = False                     # the cloud answers the next call with a usage limit
                capacity.observe_failure("anthropic", "m", 400, CREDIT)
                await rec.finish(False, error="credit balance is too low", interrupted=True)
                raise RuntimeError("Your credit balance is too low to access the Anthropic API")
        await rec.after_round([(TC("write_file", path=f"s{step}.py", content="x"), f"Wrote s{step}.py")])
        await rec.finish(True, f"step {step} done on {where}")
        return f"{step} on {where}"

    async def scenario():
        sb = _swarm(call, board)
        steps = [_step(i) for i in range(1, 7)]
        for s in steps:
            s.prompt = lambda _o, _i=s.id[1:]: f"do {_i}"
        task = asyncio.ensure_future(sb.run(steps))
        await asyncio.wait_for(in_flight_started.wait(), 10)
        for _ in range(500):                                  # the cap lands while step 5 is running on the cloud
            if governor.effective_state() == "local-only":
                break
            await asyncio.sleep(0.01)
        assert governor.effective_state() == "local-only"
        await asyncio.sleep(0.2)
        assert not task.done(), "the run is waiting for the in-flight cloud step: nothing was interrupted"
        release_in_flight.set()
        return await asyncio.wait_for(task, 20)

    try:
        out = run(scenario())
        tasks = board.list_tasks("abp/swarm-7")
    finally:
        board.close()
    # no task was dropped
    assert len(out) == 6 and {o["status"] for o in out.values()} == {"success"}, out
    assert sorted(t["state"] for t in tasks) == ["done"] * 6
    assert out["s5"]["result"] == "5 on cloud", "the step in flight when the cap hit finished on the cloud, untouched"
    assert out["s3"]["result"] == "3 on local", "the step that hit the cap was handed back and finished locally"
    assert ran_on["3"] == ["cloud", "local", "resumed-from-pack"] and ran_on["5"] == ["cloud"]
    for i in (1, 2, 4, 6):
        assert "local" in ran_on[str(i)] or ran_on[str(i)] == ["cloud"]
    # packs for every task, all finished, the handed-back one carries its history
    packs = {p["task_id"]: p for p in handoff.list_packs()}
    assert len(packs) == 6 and {p["status"] for p in packs.values()} == {"finished"}
    t3 = next(t for t in tasks if t["title"].endswith("bot3"))
    pack3 = handoff.read(t3["id"])
    assert "s3.py" in pack3.files_touched and any("resumed on local/m" in n for n in pack3.notes)
    # state history: usage cap -> local-only
    assert governor.get_state()["state"] == "local-only"

    # the cloud comes back: the probe finds out, local-only -> hybrid probation -> cloud-led
    answers = iter([Outcome(False, "usage_cap", "still out"), Outcome(True, "ok"), Outcome(True, "ok")])

    async def prober(provider, model, timeout_s):
        return next(answers)

    probe.set_prober(prober)
    r1 = run(probe.probe_once(force=True))
    assert r1["state"]["state"] == "local-only" and r1["outcomes"][0]["status"] == "usage_cap"
    r2 = run(probe.probe_once(force=True))
    assert r2["state"]["state"] == "hybrid" and r2["state"]["auto"]["probation"]
    r3 = run(probe.probe_once(force=True))
    assert r3["state"]["state"] == "cloud-led"
    assert [(h["kind"], h["to"]) for h in reversed(governor.history(10))] == [
        ("usage_cap", "local-only"), ("probe_failed", "local-only"), ("probe_ok", "hybrid"), ("recovered", "cloud-led")]
    # LF5 hook: the feed has the story a digest needs
    got = events.recent(since)
    assert [e["kind"] for e in got] == ["capacity.changed", "capacity.probe", "capacity.changed", "capacity.recovered"]
    assert "recovered" in events.digest_lines(since)[-1]


# ---- API, CLI, MCP ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    return TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}


def test_the_api_shows_the_probe_policy_events_and_pack_status(client):
    c, h = client
    assert c.get("/api/capacity/probe").status_code == 401 and c.post("/api/capacity/probe", json={}).status_code == 401
    s = c.get("/api/capacity/state", headers=h).json()
    assert s["probe"]["enabled"] and s["probe"]["last"] is None
    assert c.post("/api/capacity/probe", headers=h, json={"force": True}).json()["ran"] is False, "cloud-led: nothing to probe"
    capped(now=time.time())

    async def prober(provider, model, timeout_s):
        return Outcome(True, "ok")

    probe.set_prober(prober)
    r = c.post("/api/capacity/probe", headers=h, json={"force": True}).json()
    assert r["ran"] and r["outcomes"][0]["ok"] and r["state"] == "hybrid" and r["probe"]["last"]["ok"]
    assert c.get("/api/capacity/probe", headers=h).json()["last"]["provider"] == "anthropic"
    pol = c.get("/api/capacity/policy", headers=h).json()
    assert pol["state"] == "hybrid" and "capacity:cloud" in pol["capabilities"] and "capacity:local" in pol["capabilities"]
    ev = c.get("/api/capacity/events", headers=h).json()
    assert ev["events"] and ev["lines"] and ev["events"][-1]["to"] == "hybrid"
    handoff.write(handoff.HandoffPack(task_id="api-2", goal="g", status="interrupted", files_touched=["a", "b"]))
    pk = [p for p in c.get("/api/capacity/handoff", headers=h).json()["packs"] if p["task_id"] == "api-2"][0]
    assert pk["status"] == "interrupted" and pk["files"] == 2
    governor.set_override("local-only", now=time.time())
    assert c.get("/api/capacity/policy", headers=h).json()["capabilities"].count("capacity:cloud") == 0


def test_the_cli_and_mcp_reach_the_new_routes(client, capsys, monkeypatch):
    from abp_cli import __main__ as cli
    from bot import mcp_server

    c, h = client

    class Fake:
        async def _request(self, method, path, params=None, json=None):
            r = c.request(method, path, headers=h, params=params, json=json)
            r.raise_for_status()
            return r.json()

    class Args:
        json = False
        limit = 5
        capacity_cmd = "probe"
        reason = ""

    capped(now=time.time())

    async def prober(provider, model, timeout_s):
        return Outcome(False, "usage_cap", "no")

    probe.set_prober(prober)
    a = Args()
    assert run(cli._capacity(a, Fake())) == 0
    assert "anthropic/m1 usage_cap" in capsys.readouterr().out
    a.capacity_cmd = "status"
    assert run(cli._capacity(a, Fake())) == 0
    assert "recovery probe: last usage_cap" in capsys.readouterr().out
    a.capacity_cmd = "policy"
    assert run(cli._capacity(a, Fake())) == 0
    assert "state local-only" in capsys.readouterr().out
    handoff.write(handoff.HandoffPack(task_id="cli-pack", goal="g"))
    a.capacity_cmd = "packs"
    assert run(cli._capacity(a, Fake())) == 0
    assert "cli-pack" in capsys.readouterr().out

    seen = []

    async def fake(method, path, timeout=15.0, **kw):
        seen.append((method, path, kw))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_request", fake)
    run(mcp_server.capacity_probe())
    run(mcp_server.capacity_policy())
    assert [(m, p) for m, p, _ in seen] == [("POST", "/api/capacity/probe"), ("GET", "/api/capacity/policy")]
    assert seen[0][2]["json"] == {"force": True}


def test_local_candidates_leave_out_embedding_models(monkeypatch):
    from bot import model_router
    from bot.capacity import routing

    monkeypatch.setattr(routing, "is_local_ref", lambda r: True)
    monkeypatch.setattr(model_router, "candidate_models", lambda: [
        "modelmistress/Qwen/Qwen3-Embedding-0.6B-GGUF:Qwen3-Embedding-0.6B-Q8_0", "ollama/nomic-embed-text",
        "ollama/bge-reranker-v2", "modelmistress/ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF:Qwen3-TTS-12Hz-1.7B-Base-Q8_0",
        "modelmistress/Qwen/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M"])
    assert routing.local_candidates() == ["modelmistress/Qwen/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M"]
