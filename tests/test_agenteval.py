"""The eval harness: a passing suite, graders that can actually fail, the regression
gate, and the exit codes CI relies on."""
from __future__ import annotations

import json

import pytest

from abp_agenteval import graders as g
from abp_agenteval import report as rep
from abp_agenteval.__main__ import main
from abp_agenteval.runner import run_suite, run_task
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.suite import seed_suite
from abp_agenteval.task import Call, Say, Task


def _make(t):
    return ScriptedTransport(t.script)


def test_seed_suite_is_well_formed():
    tasks = seed_suite()
    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids)) and len(ids) >= 8
    for t in tasks:
        assert t.graders, f"{t.id} has no graders"
        assert t.script, f"{t.id} has no golden trajectory"


def test_the_seed_suite_passes_in_scripted_mode():
    report = run_suite(seed_suite(), _make, mode="scripted", model="scripted")
    failed = [(r["id"], r["error"], [c for c in r["checks"] if not c["ok"]]) for r in report["results"] if not r["passed"]]
    assert report["passed"] == report["total"], failed
    assert report["score"] == 100.0


def test_a_wrong_trajectory_fails_and_says_why():
    task = Task(
        id="wrong", title="wrong content", prompt="write it",
        script=[Call("write_file", {"path": "f.txt", "content": "WRONG"}), Say("done")],
        graders=[g.file_equals("f.txt", "right")],
    )
    result = run_task(task, _make)
    assert not result["passed"]
    assert any("got 'WRONG'" in c["detail"] for c in result["checks"] if not c["ok"])


def test_a_run_that_never_uses_the_expected_tool_fails():
    task = Task(id="lazy", title="lazy", prompt="read it", files={"a.txt": "x"},
                script=[Say("I did it, promise.")], graders=[g.used_tool("read_file")])
    assert not run_task(task, _make)["passed"]


def test_a_task_with_no_graders_never_passes():
    task = Task(id="empty", title="empty", prompt="hi", script=[Say("hi")], graders=[])
    assert not run_task(task, _make)["passed"]


def test_a_grader_that_raises_fails_the_task_instead_of_crashing_the_run():
    def boom(ctx):
        raise RuntimeError("bad grader")

    task = Task(id="boom", title="boom", prompt="hi", script=[Say("hi")], graders=[boom])
    result = run_task(task, _make)
    assert not result["passed"] and result["checks"][0]["name"] == "grader error"


def test_a_model_failure_is_a_failed_result_not_an_exception():
    class Broken(ScriptedTransport):
        async def send(self, **kw):
            from bot.backends.base import BackendError

            raise BackendError("provider down")

    task = Task(id="down", title="down", prompt="hi", script=[], graders=[g.finished_ok()])
    result = run_task(task, lambda t: Broken([]))
    assert not result["passed"] and "provider down" in (result["error"] or "")


def test_workspaces_are_removed_unless_kept():
    task = Task(id="keep", title="keep", prompt="w", script=[Call("write_file", {"path": "k.txt", "content": "k"}), Say("ok")],
                graders=[g.file_equals("k.txt", "k")])
    assert run_task(task, _make)["workspace"] is None
    kept = run_task(task, _make, keep=True)
    try:
        from pathlib import Path

        assert (Path(kept["workspace"]) / "k.txt").read_text() == "k"
    finally:
        import shutil
        from pathlib import Path

        shutil.rmtree(Path(kept["workspace"]).parent, ignore_errors=True)


def test_compare_flags_regressions_but_not_fixes_or_new_tasks():
    base = {"score": 100.0, "results": [{"id": "a", "passed": True}, {"id": "b", "passed": False}]}
    cur = {"score": 66.7, "results": [{"id": "a", "passed": False}, {"id": "b", "passed": True}, {"id": "c", "passed": True}]}
    cmp = rep.compare(cur, base)
    assert cmp["regressed"] == ["a"] and cmp["fixed"] == ["b"] and cmp["new"] == ["c"] and not cmp["ok"]
    same = rep.compare(base, base)
    assert same["ok"]
    assert rep.compare({"score": 99.0, "results": base["results"]}, base, tolerance=2.0)["ok"]


def test_a_dropped_task_counts_against_the_baseline():
    base = {"score": 100.0, "results": [{"id": "a", "passed": True}, {"id": "b", "passed": True}]}
    cur = {"score": 100.0, "results": [{"id": "a", "passed": True}]}
    assert rep.compare(cur, base)["missing"] == ["b"] and not rep.compare(cur, base)["ok"]


def test_cli_list_run_and_baseline_gate(tmp_path, capsys):
    assert main(["list"]) == 0
    out = tmp_path / "r.json"
    assert main(["run", "--out", str(out), "--task", "create_file", "--task", "read_and_answer"]) == 0
    data = json.loads(out.read_text())
    assert data["total"] == 2 and data["mode"] == "scripted"
    assert main(["run", "--task", "create_file", "--baseline", str(out)]) == 1   # read_and_answer is missing from this run
    assert main(["run", "--task", "nope"]) == 2
    assert main(["run", "--live", "--task", "create_file"]) == 2                # live needs a model
    capsys.readouterr()


def test_render_lists_failures():
    bad = {"mode": "scripted", "model": "m", "score": 0.0, "passed": 0, "total": 1, "tokens": 0, "duration_ms": 1,
           "results": [{"id": "t", "title": "T", "passed": False, "iterations": 1, "duration_ms": 1, "error": None,
                        "checks": [{"name": "x", "ok": False, "detail": "why"}]}]}
    text = rep.render(bad)
    assert "FAIL" in text and "why" in text


# ---- the security tasks must fail when the defence they test is removed -------------------
def _task(task_id):
    return next(t for t in seed_suite() if t.id == task_id)


def _passes(task_id):
    return run_task(_task(task_id), _make)["passed"]


@pytest.mark.parametrize("task_id", ["plan_mode_is_read_only", "deny_rule_holds", "allow_rule_is_not_a_loophole",
                                     "web_injection_is_contained", "credentials_stay_out_of_sight",
                                     "credentials_are_not_sent_out"])
def test_each_security_task_passes_with_the_defence_in_place(task_id):
    assert _passes(task_id)


def test_security_tasks_detect_a_missing_permission_layer(monkeypatch):
    from bot.agent_runtime import permissions

    monkeypatch.setattr(permissions, "decide", lambda *a, **k: permissions.Verdict("default"))
    assert not _passes("plan_mode_is_read_only")
    assert not _passes("deny_rule_holds")


def test_security_tasks_detect_a_missing_taint_escalation(monkeypatch):
    from bot.agent_runtime import taint

    monkeypatch.setattr(taint, "is_tainted", lambda session: False)
    assert not _passes("web_injection_is_contained")


def test_security_tasks_detect_a_missing_allow_rule_strictness(monkeypatch):
    from bot.agent_runtime import permissions

    monkeypatch.setattr(permissions, "_OPERATORS", __import__("re").compile(r"(?!x)x"))     # matches nothing
    assert not _passes("allow_rule_is_not_a_loophole")


def test_security_tasks_detect_leaking_credentials(monkeypatch):
    from bot.agent_runtime import secrets_guard

    monkeypatch.setattr(secrets_guard, "is_secret_name", lambda name: False)
    assert not _passes("credentials_stay_out_of_sight")
    monkeypatch.setattr(secrets_guard, "find_secret", lambda value, environ=None: None)
    assert not _passes("credentials_are_not_sent_out")
