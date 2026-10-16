"""LF4 phase 2a: the conductor's confined workers (parallel, audited, shell only inside an AppContainer), and the durable `blocked`
state its escalations use. Fakes for the model and the agent; the AppContainer tests run for real on Windows."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_conductor as base
from test_conductor import TEST_CLI, git, run_async

from bot import coordination
from bot.conductor import confine, llm, run, store
from bot.coordination import LocalBoard
from bot.coordination.model import BoardError

repo = base.repo                      # the temporary ABP-like git repository fixture of the phase 1 tests
_clean = base._clean
windows_only = pytest.mark.skipif(os.name != "nt", reason="AppContainers only exist on Windows")


def two_tasks(*ids, deps=None):
    deps = deps or {}
    return json.dumps({"tasks": [{"id": i, "title": f"Task {i}", "depends_on": deps.get(i, []),
                                  "proof": "python -m pytest tests -q"} for i in ids]}), "m/x"


def model_for(*ids, deps=None):
    async def model(prompt, **kw):
        return two_tasks(*ids, deps=deps)
    return model


def make_cond(repo, tmp_path, worker, board, **kw):
    cond = run.Conductor(repo, worker, board=board, workdir=tmp_path / "wt", **kw)
    cond.cfg = {**cond.cfg, "check_ruff": False, "public_repo": False}
    return cond


# ------------------------------------------------------------------ the board's blocked state
def test_blocked_is_durable_unleased_and_unblocks_to_open(tmp_path):
    now = [1_000_000]
    board = LocalBoard(tmp_path / "b.db", clock=lambda: now[0])
    try:
        t, _ = board.create_task("b", "needs a decision", key="k", scope={"resources": ["worktree:/w/1"]}, max_attempts=2)
        got = board.claim(t["id"], "w1", lease_s=10)
        assert board.list_locks()
        out = board.set_state(t["id"], "w1", got["fence"], "blocked", result={"question": "may I?"}, note="BLOCKED: may I?")
        assert out["state"] == "blocked" and out["lease_until"] is None and out["owner"] == "w1"
        assert board.list_locks() == []                                  # the scope is free for others
        now[0] += 10 * 3600 * 1000                                       # hours later: the sweeper has nothing to requeue or fail
        assert board.get_task(t["id"])["state"] == "blocked" and board.get_task(t["id"])["attempts"] == 0
        assert board.summary("b")["boards"]["b"]["counts"]["blocked"] == 1
        again, deduped = board.create_task("b", "needs a decision", key="k")
        assert deduped and again["id"] == t["id"]                         # re-planning joins it instead of duplicating
        with pytest.raises(BoardError) as e:                              # a blocked task has no owner session any more
            board.heartbeat(t["id"], "w1", got["fence"])
        assert e.value.code == "NOT_OWNER"
        with pytest.raises(BoardError):
            board.unblock(t["id"], by="")
        back = board.unblock(t["id"], by="owner", note="yes, remove it")
        assert back["state"] == "open" and back["owner"] is None and "unblocked by owner" in back["note"]
        with pytest.raises(BoardError) as e:
            board.unblock(t["id"], by="owner")
        assert e.value.code == "NOT_BLOCKED"
        assert board.next("b", "w2") is not None                          # and it can be claimed again
    finally:
        board.close()


def test_agentsentinel_has_no_blocked_state_so_blocked_maps_to_failed_with_a_note():
    from bot.coordination.sentinel import SentinelBoard

    sent = {}

    class Fake(SentinelBoard):
        def __init__(self):
            pass

        def _call(self, method, path, body=None, **kw):
            sent.update(body)
            return SimpleNamespace(json=lambda: {"state": body["state"], "note": body.get("note")})

    Fake().set_state("t1", "a", 3, "blocked", result={"question": "q"}, note="ESCALATED: two rejections")
    assert sent["state"] == "failed" and sent["note"].startswith("BLOCKED: ESCALATED") and sent["result"]["blocked"] is True
    with pytest.raises(BoardError) as e:
        SentinelBoard.unblock(Fake(), "t1", by="o")
    assert e.value.code == "NOT_BLOCKED"


# --------------------------------------------------------------------- escalations -> blocked
def test_a_rule_question_escalates_to_blocked_with_the_minimum_context_and_unblocks(repo, tmp_path):
    async def drive(prompt, wt):
        cli = Path(wt, "cli.py")
        cli.write_text(cli.read_text(encoding="utf-8").replace('    s.add_argument("--verbose", action="store_true")\n', ""),
                       encoding="utf-8", newline="\n")
        Path(wt, "tests/test_cli.py").write_text(TEST_CLI + "\n# touched\n", encoding="utf-8", newline="\n")
        return "dropped a flag"

    llm.set_completer(model_for("bad", "after", deps={"after": ["bad"]}))
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = make_cond(repo, tmp_path, run.AgentLoopWorker(repo, drive=drive), board)
        plan = run_async(cond.plan("remove the verbose flag, then more"))
        report = run_async(cond.run(plan))
        assert report.escalated == ["bad"] and report.not_run == ["after"]
        task = next(t for t in board.list_tasks(limit=20) if t["key"] == plan.tasks[0].key)
        assert task["state"] == "blocked" and task["note"].startswith("ESCALATED") and task["lease_until"] is None
        full = board.get_task(task["id"])
        ctx = full["result"]["context"]
        assert ctx["kind"] == "rule_decision" and "removed" in ctx["question"].lower()
        assert "--verbose" in ctx["failing_check"] and "no_feature_removed" in ctx["failing_check"]
        assert "-    s.add_argument(\"--verbose\"" in ctx["diff_excerpt"] and len(json.dumps(ctx)) < 5000
        esc = store.open_escalation(plan.id, plan.tasks[0].key)
        assert esc and esc["data"]["blocked"]["kind"] == "rule_decision"
        assert next(t for t in board.list_tasks(limit=20) if t["key"] == plan.tasks[1].key)["state"] == "cancelled"

        # the decision: the next run starts the task again with a fresh rejection count
        assert store.rejections(plan.id, plan.tasks[0].key) == 2
        out = cond.unblock(plan.id, plan.tasks[0].key, "owner", "keep the flag")
        assert out["board"] == "open" and store.rejections(plan.id, plan.tasks[0].key) == 0
        assert store.open_escalation(plan.id, plan.tasks[0].key) is None
        assert next(t for t in board.list_tasks(limit=20) if t["key"] == plan.tasks[0].key)["state"] == "open"
        with pytest.raises(KeyError):
            cond.unblock(plan.id, plan.tasks[0].key, "owner")
        with pytest.raises(ValueError):
            cond.unblock(plan.id, plan.tasks[0].key, "")
    finally:
        board.close()


def test_a_worker_that_keeps_ending_without_a_change_is_stuck_and_blocked(repo, tmp_path):
    seen = []

    async def drive(prompt, wt):
        seen.append(prompt)
        return SimpleNamespace(reply="I looked around", tool_calls=[{"tool": "read_file", "ok": True}], tokens=42, iterations=7,
                               stopped="steps")

    llm.set_completer(model_for("idle"))
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = make_cond(repo, tmp_path, run.AgentLoopWorker(repo, drive=drive), board)
        plan = run_async(cond.plan("do nothing useful"))
        report = run_async(cond.run(plan))
        assert report.escalated == ["idle"] and len(seen) == 2 and "changed nothing" in seen[1]
        task = board.list_tasks(limit=5)[0]
        ctx = board.get_task(task["id"])["result"]["context"]
        assert task["state"] == "blocked" and ctx["kind"] == "stuck" and "split" in ctx["question"]
        assert store.list_reviews(plan.id) == []                          # nothing was reviewed: there was no change
        # every tool call of every attempt is on record
        rows = store.list_tool_calls(plan.id)
        assert [r["attempt"] for r in rows] == [1, 2] and rows[0]["calls"] == [{"tool": "read_file", "ok": True}]
        assert "tokens=42" in rows[0]["summary"] and "stopped=steps" in rows[0]["summary"]
    finally:
        board.close()


# --------------------------------------------------------- parallel workers, release on failure
def test_n_workers_pull_tasks_in_parallel_in_their_own_worktrees(repo, tmp_path):
    active, peak, lock = [0], [0], threading.Lock()
    worktrees = []

    async def drive(prompt, wt):
        worktrees.append(str(wt))
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.6)
        with lock:
            active[0] -= 1
        name = "m" + Path(wt).name.replace("-", "_")
        Path(wt, f"{name}.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
        Path(wt, "tests", f"test_{name}.py").write_text(f"from {name} import X\n\n\ndef test_x():\n    assert X\n", encoding="utf-8", newline="\n")
        return "ok"

    llm.set_completer(model_for("a", "b", "c"))
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = make_cond(repo, tmp_path, run.AgentLoopWorker(repo, drive=drive), board, workers=3)
        plan = run_async(cond.plan("three independent things"))
        report = run_async(cond.run(plan))
        assert len(report.accepted) == 3 and not report.escalated and peak[0] >= 2, json.dumps(store.list_reviews(plan.id)[0]["report"]["findings"])
        assert len(set(worktrees)) == 3 and all(str(tmp_path / "wt").replace("\\", "/") in w.replace("\\", "/") for w in worktrees)
        agents = {p["agent"] for p in board.list_presence()}
        assert len([a for a in agents if ":w" in a]) == 3                  # three workers pulled with `next`
    finally:
        board.close()


def test_no_local_model_releases_the_task_instead_of_failing_it(repo, tmp_path):
    calls = []

    async def worker(spec, feedback):
        calls.append(1)
        if len(calls) == 1:
            raise llm.NoLocalModel("hub is starting")
        Path(spec["worktree"]).mkdir(parents=True, exist_ok=True)
        git(repo, "branch", spec["branch"], "main")
        return {"branch": spec["branch"], "start": "main", "empty": True}

    llm.set_completer(model_for("one"))
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = make_cond(repo, tmp_path, worker, board)
        cond.cfg = {**cond.cfg, "max_rejections": 1}
        plan = run_async(cond.plan("needs a model"))
        report = run_async(cond.run(plan))
        assert len(calls) == 2                                           # released after the first, taken again
        assert report.escalated == ["one"]                               # (the fake then produced no change: stuck, blocked)
        assert board.list_tasks(limit=5)[0]["attempts"] == 0
    finally:
        board.close()


# --------------------------------------------------------------- the confined shell
def test_the_sandbox_shell_is_refused_not_degraded_where_there_is_no_appcontainer(repo, monkeypatch):
    monkeypatch.setattr(confine, "supported", lambda: False)
    with pytest.raises(confine.SandboxUnavailable, match="AppContainer"):
        run.AgentLoopWorker(repo, shell="sandbox")
    with pytest.raises(confine.SandboxUnavailable):
        confine.verify(repo)
    with pytest.raises(ValueError):
        run.AgentLoopWorker(repo, shell="yolo")
    assert run.AgentLoopWorker(repo).shell == "none"                      # the default: no shell tool at all


def test_the_cli_gained_shell_and_workers_and_unblock():
    from abp_cli import __main__ as cli

    a = cli._parser().parse_args(["conductor", "run", "g", "--repo", "r", "--shell", "sandbox", "--workers", "3"])
    assert a.shell == "sandbox" and a.workers == 3
    assert cli._parser().parse_args(["conductor", "run", "g", "--repo", "r"]).shell == "none"
    u = cli._parser().parse_args(["conductor", "unblock", "p1", "k1", "--by", "owner"])
    assert u.plan_id == "p1" and u.task_key == "k1"


@windows_only
def test_the_conductors_sandbox_blocks_home_outside_paths_and_the_network(repo, tmp_path):
    ws = tmp_path / "wt"
    ws.mkdir()
    # the worker's own preflight: the probes pass in the conductor's sandbox settings (windows_job backend, network none)
    confine.verify(ws)
    # and they are not vacuous: the same probes against an unconfined shell find the leaks
    from abp_agenteval import factory

    from bot.agent_runtime import sandbox

    with factory.agent_environment({"sandbox": {"backend": "local", "network": "allow"}}):
        assert sandbox.network() == "allow"
        leaks = confine.probe(ws)
    assert any("home" in p or "outside" in p or "loopback" in p for p in leaks), leaks


@windows_only
def test_a_conductor_worker_in_sandbox_mode_runs_its_agent_shell_confined(repo, tmp_path):
    """The AgentLoopWorker path end to end with a scripted agent whose only act is run_shell, through the real tool and sandbox."""
    from abp_agenteval import factory
    from bot.agent_runtime import toolspec, tools

    outside = tmp_path / "outside.txt"
    outside.write_text("not-for-the-agent", encoding="utf-8")
    seen = {}

    async def drive(prompt, wt):
        settings = factory.agent_settings(model="m", shell="sandbox", sandbox_paths=confine.sandbox_paths())
        token = toolspec.session_var.set("conductor-test")
        try:
            with factory.agent_environment(settings):
                seen["read"] = await tools.execute_tool("run_shell", {"command": f'type "{outside}"'}, workspace=Path(wt))
                seen["write"] = await tools.execute_tool("run_shell", {"command": "echo made> made.txt"}, workspace=Path(wt))
        finally:
            toolspec.session_var.reset(token)
        Path(wt, "made_ok.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
        return SimpleNamespace(reply="done", tool_calls=[{"tool": "run_shell"}], tokens=1, iterations=1, stopped="")

    worker = run.AgentLoopWorker(repo, drive=drive, shell="sandbox")
    spec = {"title": "t", "goal": "g", "key": "k", "branch": "conductor/t", "worktree": str(tmp_path / "wt" / "t"), "base": "main"}
    res = run_async(worker(spec, ""))
    assert "not-for-the-agent" not in seen["read"]
    assert (Path(res["worktree"]) / "made.txt").read_text().strip() == "made"
    assert res["shell"] == "sandbox" and res["tool_calls"] == [{"tool": "run_shell"}] and res["empty"] is False
    assert worker._verified is True


# ------------------------------------------------ a local model is never swapped for a cloud one
def test_a_local_ref_never_resolves_to_the_anthropic_transport(monkeypatch):
    from bot.modelmistress import client as mm

    for bad in ("", "gpt", "anthropic/claude-x", "/x", "openai/gpt-x"):
        with pytest.raises(llm.NoLocalModel):
            llm.transport_for_ref(bad)
    monkeypatch.setattr(mm, "find", lambda *a, **k: mm.Hub("http://127.0.0.1:9", "tok", "0.3"))
    monkeypatch.setenv("ABP_NO_MODULE_PROVIDERS", "1")
    transport, name = llm.transport_for_ref("modelmistress/vendor/model-GGUF:Q4")
    assert name == "vendor/model-GGUF:Q4" and "127.0.0.1:9" in str(transport.base_url) and type(transport).__name__ != "AnthropicTransport"
    monkeypatch.setattr(mm, "find", lambda *a, **k: None)
    with pytest.raises(llm.NoLocalModel, match="hub running"):
        llm.transport_for_ref("modelmistress/vendor/model")


def test_a_model_that_did_not_answer_is_a_released_task_not_a_stuck_worker(repo, tmp_path):
    async def drive(prompt, wt):
        return SimpleNamespace(ok=False, error="connection refused", reply="", tool_calls=[], tokens=0, iterations=0, stopped="")

    worker = run.AgentLoopWorker(repo, drive=drive)
    spec = {"title": "t", "goal": "g", "key": "k", "branch": "conductor/t2", "worktree": str(tmp_path / "wt" / "t2"), "base": "main"}
    with pytest.raises(llm.WorkerModelError, match="did not answer") as e:
        run_async(worker(spec, ""))
    assert e.value.release_task is True
