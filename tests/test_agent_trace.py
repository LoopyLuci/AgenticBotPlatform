"""Agent traces record what happened without recording what was said."""
from __future__ import annotations

import asyncio

from abp_agenteval import graders as g
from abp_agenteval.runner import run_task
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say, Task
from bot.agent_runtime import trace

SECRET_PROMPT = "please handle the quarterly-widget-report for Acme"
SECRET_CONTENT = "salary-table-contents-do-not-leak"


def _all_events(run_id):
    return trace.run_events(run_id)


def test_a_run_records_start_llm_calls_tool_calls_and_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "t.db"))
    task = Task(id="t", title="t", prompt=SECRET_PROMPT, files={"data.txt": SECRET_CONTENT},
                script=[Call("read_file", {"path": "data.txt"}), Say("done")], graders=[g.finished_ok()])
    result = run_task(task, lambda t: ScriptedTransport(t.script))
    assert result["passed"] and result["run_id"]
    assert result["iterations"] == 2 and result["tool_counts"] == {"read_file": 1}


def test_a_trace_never_contains_prompts_replies_or_file_contents(tmp_path, monkeypatch):
    seen = {}

    def keep_trace(t):
        return ScriptedTransport(t.script)

    task = Task(id="leak", title="leak", prompt=SECRET_PROMPT, files={"data.txt": SECRET_CONTENT},
                script=[Call("read_file", {"path": "data.txt"}), Say("The report mentions " + SECRET_CONTENT)],
                graders=[g.finished_ok()])
    # Run inside the runner's isolated store, then read it back before it is torn down.
    import abp_agenteval.runner as runner

    real = runner.isolated_environment

    import contextlib

    @contextlib.contextmanager
    def spy(root, approvals, *rest):
        with real(root, approvals, *rest):
            yield
            seen["events"] = trace.get_store().events(limit=5000)

    monkeypatch.setattr(runner, "isolated_environment", spy)
    run_task(task, keep_trace)
    blob = str(seen["events"])
    assert seen["events"], "nothing was recorded"
    assert SECRET_PROMPT not in blob and SECRET_CONTENT not in blob
    kinds = {e["kind"] for e in seen["events"]}
    assert {"agent.run.start", "llm.call", "tool.call", "agent.run.end"} <= kinds


def test_tool_call_target_is_short_and_redacted(tmp_path):
    store = trace.get_store(tmp_path / "x.db")
    t = trace.Trace(store=store, agent="a", model="m")
    t.tool_call("run_shell", {"command": "curl -H 'Authorization: Bearer abcdef1234567890' https://x " + "y" * 500},
                status="ok", duration_ms=5, output="out")
    ev = [e for e in store.events() if e["kind"] == "tool.call"][0]["data"]
    assert "abcdef1234567890" not in ev["target"]
    assert len(ev["target"]) <= 500
    assert ev["output_chars"] == 3


def test_the_store_only_accepts_declared_fields(tmp_path):
    store = trace.get_store(tmp_path / "y.db")
    t = trace.Trace(store=store, agent="a", model="m", prompt="secret prompt", api_key="k")
    start = [e for e in store.events() if e["kind"] == "agent.run.start"][0]["data"]
    assert "prompt" not in start and "api_key" not in start and start["agent"] == "a"


def test_the_chain_verifies_after_a_run(tmp_path):
    store = trace.get_store(tmp_path / "z.db")
    with trace.Trace(store=store, agent="a", model="m") as t:
        t.llm_call(model="m", duration_ms=1, tokens=5)
        t.end("ok")
    assert store.verify_chain()["ok"]


def test_nested_runs_link_to_their_parent(tmp_path):
    store = trace.get_store(tmp_path / "n.db")
    with trace.Trace(store=store, agent="parent", model="m") as parent:
        with trace.Trace(store=store, agent="child", model="m") as child:
            assert child.parent_run == parent.run_id
    starts = {e["data"]["agent"]: e["data"] for e in store.events(kind="agent.run.start")}
    assert starts["child"]["parent_run"] == parent.run_id and "parent_run" not in starts["parent"]


def test_tracing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ABP_AGENT_TRACE", "0")
    run = trace.begin(agent="a", model="m")
    assert run is trace.NULL
    run.llm_call(model="m", duration_ms=1)     # a no-op object: call sites need no guards
    assert trace.active() is trace.NULL


def test_a_broken_store_never_breaks_the_run(tmp_path):
    # A directory where the database file should be: every write fails.
    bad = tmp_path / "isadir.db"
    bad.mkdir()
    t = trace.Trace(store=trace.get_store(bad), agent="a", model="m")
    t.llm_call(model="m", duration_ms=1)
    t.tool_call("read_file", {"path": "x"}, status="ok", duration_ms=1)
    t.end("ok")   # nothing raised


def test_denied_and_failed_calls_are_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "c.db"))
    task = Task(id="d", title="d", prompt="p", approvals={"run_shell": "deny"},
                script=[Call("run_shell", {"command": "echo x"}, more=(("read_file", {"path": "missing.txt"}),)), Say("ok")],
                graders=[g.finished_ok(), g.tool_status("run_shell", "denied"), g.tool_status("read_file", "failed")])
    result = run_task(task, lambda t: ScriptedTransport(t.script))
    assert result["passed"], result["checks"]
    assert result["denied"] == 1
