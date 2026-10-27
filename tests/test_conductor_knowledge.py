"""LF7: the Factory learns from earlier runs (bot/conductor/knowledge.py). Lessons recorded per task outcome come back to the
planner and to a worker on a later, similar run; each part switches off; the dataset export carries rejections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import coordination
from bot.conductor import knowledge, llm, run
from bot.coordination import LocalBoard
from tests.test_conductor import TEST_CLI, repo, run_async  # noqa: F401 - the repo fixture

OFF_FABRIC = {"knowledge": {**knowledge.DEFAULTS, "to_memoryfabric": False}}


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    from bot.conductor import store

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "conductor.db")
    llm.set_completer(None)
    yield
    llm.set_completer(None)
    coordination.set_board(None)


def test_lessons_are_recalled_by_file_and_by_title_failures_first():
    knowledge.record("abp", "p1", "k1", "Add a --json flag to status", "accepted", goal="json output", files=["cli.py"],
                     text="added json_flag", cfg=OFF_FABRIC)
    knowledge.record("abp", "p1", "k2", "Add a --json flag to status", "rejected", goal="json output", files=["cli.py"],
                     rules=["no_feature_removal"], text="removed --verbose", cfg=OFF_FABRIC)
    knowledge.record("abp", "p2", "k3", "Rewrite the README intro", "accepted", files=["README.md"], cfg=OFF_FABRIC)
    knowledge.record("other", "p3", "k4", "Add a --json flag", "rejected", files=["cli.py"], cfg=OFF_FABRIC)

    rows = knowledge.related("abp", "json flag for the status command", ["cli.py"])
    assert [r["outcome"] for r in rows] == ["rejected", "accepted"]          # README and the other repo do not come back
    block = knowledge.task_block("abp", "Add a --json flag", files=["cli.py"], cfg=OFF_FABRIC)
    assert "removed --verbose" in block and block.index("[rejected]") < block.index("[accepted]")
    assert knowledge.task_block("abp", "Add a --json flag", files=["cli.py"], plan_id="p1", cfg=OFF_FABRIC) == ""


def test_planner_block_has_similar_plans_failing_rules_and_owner_rules(monkeypatch):
    from bot.memoryfabric import rules as fabric_rules

    monkeypatch.setattr(fabric_rules, "rules_for_prompt", lambda: "Owner rules: never remove a feature.")
    for i in range(2):
        knowledge.record("abp", f"p{i}", "k", "Drop the old flag", "rejected", goal="clean up the status command",
                         rules=["no_feature_removal"], cfg=OFF_FABRIC)
    block = knowledge.planner_block("abp", "clean up the status command output", OFF_FABRIC)
    assert "Earlier Factory plans" in block and "Drop the old flag = rejected" in block
    assert "no_feature_removal (2x)" in block and "Owner rules" in block
    off = {"knowledge": {**OFF_FABRIC["knowledge"], "owner_rules": False}}
    assert "Owner rules" not in knowledge.planner_block("abp", "clean up the status command", off)


def test_switches_turn_each_part_off():
    cfg = {"knowledge": {**OFF_FABRIC["knowledge"], "enabled": False}}
    assert knowledge.record("abp", "p", "k", "t", "accepted", cfg=cfg) == 0 and knowledge.lessons() == []
    knowledge.record("abp", "p", "k", "Add a flag", "rejected", files=["cli.py"], cfg=OFF_FABRIC)
    assert knowledge.task_block("abp", "Add a flag", files=["cli.py"], cfg={"knowledge": {"for_workers": False}}) == ""
    assert knowledge.planner_block("abp", "Add a flag", {"knowledge": {"for_planner": False}}) == ""
    assert knowledge.record("abp", "p", "k", "t", "not-an-outcome", cfg=OFF_FABRIC) == 0


def test_lessons_reach_the_memory_fabric(monkeypatch):
    from bot.memoryfabric import knowledge as kb

    seen = []
    monkeypatch.setattr(kb, "ingest", lambda *a, **kw: seen.append((a, kw)))
    knowledge.record("abp", "p", "k", "Add a flag", "rejected", files=["cli.py"], text="dropped --verbose",
                     cfg={"knowledge": knowledge.DEFAULTS})
    (args, kw), = seen
    assert args[0] == "factory" and "dropped --verbose" in args[3] and "rejected" in kw["tags"]


def test_export_pairs_a_rejection_with_the_later_fix(tmp_path):
    knowledge.record("abp", "p", "k", "Add a flag", "rejected", text="dropped --verbose", rules=["no_feature_removal"], cfg=OFF_FABRIC)
    knowledge.record("abp", "p", "k", "Add a flag", "accepted", text="kept --verbose, added --json", cfg=OFF_FABRIC)
    out = knowledge.export_dataset(tmp_path / "ds" / "x.jsonl")
    rows = [json.loads(ln) for ln in Path(out["path"]).read_text(encoding="utf-8").splitlines()]
    assert out["rows"] == 1 and rows[0]["rejection"] == "dropped --verbose" and rows[0]["fixed"].startswith("kept --verbose")


def test_a_second_factory_run_is_given_what_the_first_run_learned(repo, tmp_path, monkeypatch):  # noqa: F811
    """Run 1: the worker drops a CLI flag twice; the reviewer rejects and the task escalates. Run 2 on a similar goal: the
    planner's prompt carries run 1's plan and the failing rule, and the worker's first prompt carries the earlier rejection."""
    from bot.conductor import projectkm as _pk

    monkeypatch.setattr(_pk, "km_dir", lambda: tmp_path / "km")
    plan_prompts: list[str] = []
    worker_prompts: list[str] = []

    async def model(prompt, **kw):
        plan_prompts.append(prompt)
        return json.dumps({"tasks": [{"id": "flag", "title": "Change the status flags", "files": ["cli.py"],
                                      "proof": "python -m pytest tests -q"}]}), "m/x"

    async def drive(prompt, wt):
        worker_prompts.append(prompt)
        cli = Path(wt, "cli.py")
        cli.write_text(cli.read_text(encoding="utf-8").replace('    s.add_argument("--verbose", action="store_true")\n', ""),
                       encoding="utf-8", newline="\n")
        Path(wt, "tests/test_cli.py").write_text(TEST_CLI + "\n# touched\n", encoding="utf-8", newline="\n")
        return "changed the flags"

    from bot.conductor import projectkm
    from tests.test_conductor import git

    (Path(repo) / "AGENTS.md").write_text("# Rules\n\n## Conventions\n\n- Never drop a CLI flag of the status command: users script it.\n",
                                          encoding="utf-8")
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-q", "-m", "rules")
    projectkm._cache.clear()
    llm.set_completer(model)
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        for n, goal in enumerate(("change the status command flags", "change the status command flags again")):
            cond = run.Conductor(repo, run.AgentLoopWorker(repo, drive=drive), board=board, workdir=tmp_path / f"wt{n}")
            cond.cfg = {**cond.cfg, "check_ruff": False, "public_repo": False, "context": {**cond.cfg["context"], "use_vault": False},
                        **OFF_FABRIC}
            cond.worker.cfg = cond.cfg
            first_worker_prompt = len(worker_prompts)
            report = run_async(cond.run(run_async(cond.plan(goal)), integrate=False))
            assert report.escalated == ["flag"]
            if n == 0:
                assert "Earlier Factory plans" not in plan_prompts[-1]
                assert "earlier Factory runs learned" not in worker_prompts[first_worker_prompt]
        assert "Earlier Factory plans" in plan_prompts[-1] and "Change the status flags = rejected" in plan_prompts[-1]
        first = worker_prompts[first_worker_prompt]
        assert "earlier Factory runs learned" in first and "[rejected] Change the status flags" in first and "--verbose" in first
        assert knowledge.stats("abp")["by_outcome"]["rejected"] == 4
        # the project knowledge module (mmkm/1) reached both: the AGENTS.md rule about the status command's flags
        assert "project's knowledge module" in first and "Never drop a CLI flag" in first
        assert "Never drop a CLI flag" in plan_prompts[-1]
    finally:
        board.close()


def test_the_knowledge_api_reads_switches_audits_and_exports(monkeypatch, temp_db, tmp_path):
    from fastapi.testclient import TestClient

    from bot.config import config
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}
    knowledge.record("abp", "p", "k", "Add a flag", "rejected", files=["cli.py"], text="dropped --verbose", cfg=OFF_FABRIC)
    assert c.get("/api/conductor/knowledge").status_code == 401
    doc = c.get("/api/conductor/knowledge", headers=h).json()
    assert doc["lessons"] == 1 and set(doc["switches"]) == set(knowledge.SWITCHES)
    assert c.get("/api/conductor/knowledge/lessons", headers=h).json()["lessons"][0]["title"] == "Add a flag"
    assert "dropped --verbose" in c.get("/api/conductor/knowledge/recall", headers=h, params={"text": "Add a flag"}).json()["task"]

    changed = {}
    monkeypatch.setattr(config, "set_values", lambda ch, actor="": changed.update(ch))
    assert c.put("/api/conductor/knowledge/settings", headers=h, json={"for_workers": "no"}).status_code == 400
    assert c.put("/api/conductor/knowledge/settings", headers=h, json={}).status_code == 400
    assert c.put("/api/conductor/knowledge/settings", headers=h, json={"for_workers": False}).status_code == 200
    assert changed == {("conductor", "knowledge", "for_workers"): False}
    from bot import db

    assert db.list_audit_log(["conductor_knowledge_set"])
    from bot.agent_runtime import state

    monkeypatch.setattr(state, "state_dir", lambda *parts: tmp_path.joinpath(*parts))
    out = c.post("/api/conductor/knowledge/export", headers=h, json={}).json()
    assert out["rows"] == 1
