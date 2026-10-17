"""Memory graph: episodes joined from the trace and the handoff pack, tool/skill -> task-type edges, fused recall."""
from __future__ import annotations

import pytest

from bot.agent_runtime import trace
from bot.capacity import handoff
from bot.memory_graph import builder, recall, tasktype
from bot.memory_graph.store import Graph, db_path

GOAL_PASS = "fix the failing login test in the auth module"
GOAL_FAIL = "fix the failing login bug in the auth module"


@pytest.fixture
def store(tmp_path):
    return trace.EventStore(tmp_path / "traces.db", kinds=trace.TRACE_KINDS)


def _run(store, goal, tools, *, skills=(), task_id="", status="ok", model="m1", failed=(), workspace=None):
    """One traced run through the real Trace class; its end() fires the memory-graph hook."""
    t = trace.Trace(store=store, agent="a", model=model, task_type=tasktype.classify(goal))
    t.goal_hint, t.task_id, t.workspace = goal, task_id, workspace
    for name in tools:
        t.tool_call(name, {}, status="failed" if name in failed else "ok", duration_ms=5,
                    error="boom: exit code 2" if name in failed else "")
    for s in skills:
        t.tool_call("read_skill", {"name": s}, status="ok", duration_ms=1)
    t.llm_call(model=model, duration_ms=10, tokens=1200, usd=0.01)
    t.end(status)
    return t.run_id


def _pack(task_id, goal, tests, *, files=(), status="finished", last_error=""):
    handoff.write(handoff.HandoffPack(task_id=task_id, goal=goal, files_touched=list(files), status=status,
                                      last_error=last_error, test_status={"status": tests, "command": "pytest", "summary": ""}))


def test_trace_carries_task_type_and_skill_name(store):
    rid = _run(store, GOAL_PASS, ["grep_tool"], skills=["debugging"])
    evs = trace.run_events(rid, store)
    assert next(e for e in evs if e["kind"] == "agent.run.start")["data"]["task_type"] == "fix_bug"
    skill_calls = [e["data"] for e in evs if e["kind"] == "tool.call" and e["data"]["tool"] == "read_skill"]
    assert skill_calls[0]["skill"] == "debugging"


def test_episode_fields_and_edges(store, tmp_path):
    _pack("t-ok", GOAL_PASS, "passing", files=["auth.py"])
    rid = _run(store, GOAL_PASS, ["toolA"], skills=["S"], task_id="t-ok")
    g = Graph(db_path(store.path))
    ep = g.get_episode(rid)
    assert ep["task_type"] == "fix_bug" and ep["task_id"] == "t-ok" and ep["model"] == "m1"
    assert ep["tools"]["toolA"] == {"calls": 1, "errors": 0} and ep["skills"] == ["S"]
    assert ep["tokens"] == 1200 and ep["usd"] == 0.01 and ep["iterations"] == 1
    assert ep["outcome"] == {"status": "ok", "test_status": "passing", "score": 1.0}
    assert ep["files"] == ["auth.py"] and "login" in ep["goal_summary"]
    (edge,) = [e for e in g.edges_to("task_type", "fix_bug", "helped", "tool") if e["src_key"] == "toolA"]
    assert (edge["n"], edge["wins"], edge["fails"]) == (1, 1, 0) and edge["weight"] == pytest.approx(2 / 3)
    assert builder.build_episode(rid, trace_store=store, graph=g)["id"] == rid       # idempotent
    assert g.edges_to("task_type", "fix_bug", "helped", "tool")[0]["n"] == 1


def test_recall_ranks_the_winning_tool_and_skill_and_avoids_the_failure(store):
    _pack("t-ok", GOAL_PASS, "passing")
    _run(store, GOAL_PASS, ["toolA"], skills=["S"], task_id="t-ok")
    _pack("t-bad", GOAL_FAIL, "failing", status="failed", last_error="Traceback: KeyError 'user'")
    _run(store, GOAL_FAIL, ["toolB"], task_id="t-bad", status="failed", failed=("toolB",))
    g = Graph(db_path(store.path))
    bad_edge = [e for e in g.edges_to("task_type", "fix_bug", "helped", "tool") if e["src_key"] == "toolB"][0]
    assert (bad_edge["wins"], bad_edge["fails"]) == (0, 1)
    got = recall.recall_for_task("fix the failing login problem in auth", graph=g)
    names = [t["name"] for t in got["tools"]]
    assert names[0] == "toolA" and "toolB" not in names
    assert [s["name"] for s in got["skills"]] == ["S"]
    assert any(a["kind"] == "tool" and a["name"] == "toolB" for a in got["avoid"])
    assert any(a["kind"] == "error" for a in got["avoid"])
    assert got["episodes"][0]["outcome"] == "ok"
    assert "toolB" in recall.format_text(got) and len(recall.format_text(got, 80)) <= 80


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def check_password(user, pw):\n    return True\n", encoding="utf-8")
    (root / "views.py").write_text("from auth import check_password\n\ndef login(u, p):\n    return check_password(u, p)\n",
                                   encoding="utf-8")
    (root / "unrelated.py").write_text("def thumbnails():\n    return 1\n", encoding="utf-8")
    return root


def test_codeintel_proximity_surfaces_episodes_that_touched_referrers(store, repo):
    _pack("t-v", "adjust the signup page layout", "passing", files=["views.py"])
    _run(store, "adjust the signup page layout", ["toolV"], task_id="t-v", workspace=str(repo))
    _pack("t-u", "adjust the gallery grid", "passing", files=["unrelated.py"])
    _run(store, "adjust the gallery grid", ["toolU"], task_id="t-u", workspace=str(repo))
    g = Graph(db_path(store.path))
    ep = g.recent_episodes(5)
    assert {e["files"][0] for e in ep} == {"views.py", "unrelated.py"}
    # the symbols of the touched file are graph nodes (names only)
    assert g.edges_to("file", "views.py", "co_occurred", "symbol")
    got = recall.recall_for_task("rework check_password in auth.py", root=repo, graph=g)
    assert [e["files"] for e in got["episodes"]] == [["views.py"]]
    assert [t["name"] for t in got["tools"]][:1] == ["toolV"]
    far = recall.recall_for_task("rework check_password in auth.py", graph=g)       # no repo -> no hop
    assert far["episodes"] == []


def test_error_fix_edge_from_the_vault(store, monkeypatch):
    class FakeVault:
        def recall(self, *a, **k):
            return [{"text": "Error: boom\nFix: pin the dependency"}]

    import bot.context_vault as cv

    monkeypatch.setattr(cv, "get_vault", lambda: FakeVault())
    _pack("t-f", "fix the install error", "passing")
    _run(store, "fix the install error", ["toolF"], task_id="t-f", failed=("toolF",))
    g = Graph(db_path(store.path))
    sig = builder.error_signature("boom: exit code 2")
    assert g.edges_from("error", sig, "resolved_by")[0]["dst_key"] == "pin the dependency"
    assert g.edges_to("error", sig, "caused", "tool")[0]["src_key"] == "toolF"


def test_degrades_without_pack_codeintel_or_graph(store, tmp_path, monkeypatch):
    rid = _run(store, "explore the repo", ["toolX"], workspace=str(tmp_path / "does-not-exist"))
    g = Graph(db_path(store.path))
    ep = g.get_episode(rid)
    assert ep["files"] == [] and ep["outcome"]["test_status"] == "unknown" and ep["outcome"]["score"] == 0.7
    assert builder.build_episode("no-such-run", trace_store=store, graph=g) is None
    from bot.codeintel import store as ci_store

    monkeypatch.setattr(ci_store, "connect", lambda root: (_ for _ in ()).throw(RuntimeError("no index")))
    got = recall.recall_for_task("explore the repo", root=tmp_path, files=["a.py"], graph=Graph(tmp_path / "empty.db"))
    assert got["tools"] == [] and got["episodes"] == [] and "Nothing relevant" in recall.format_text(got)


def test_disabled_by_config_and_a_broken_hook_never_raises(store, monkeypatch):
    monkeypatch.setattr(builder, "build_episode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _run(store, "fix a thing", ["toolZ"])                  # the hook raises inside; end() must still return
    monkeypatch.undo()
    from bot.memory_graph import settings

    monkeypatch.setattr(settings, "load", lambda raw=None: {**settings.DEFAULTS, "enabled": False})
    rid = _run(store, "fix another thing", ["toolZ"])
    assert Graph(db_path(store.path)).get_episode(rid) is None


def test_memory_relevant_tool_is_registered_and_capped(store, monkeypatch):
    import asyncio

    from bot.agent_runtime import tools as agent_tools  # noqa: F401  (importing it registers the tool)
    from bot.agent_runtime import toolspec

    assert "memory_relevant" in toolspec.registered_names() and toolspec.is_read_only("memory_relevant")
    _pack("t-ok", GOAL_PASS, "passing")
    _run(store, GOAL_PASS, ["toolA"], task_id="t-ok")
    monkeypatch.setenv("ABP_MEMORY_GRAPH_DB", str(db_path(store.path)))
    handler = toolspec._registered["memory_relevant"][2]
    assert handler is not None
    out = asyncio.run(handler({"goal": GOAL_PASS}))
    assert "toolA" in out and len(out) <= 4000
