"""LF4 phase 1: the local conductor (bot/conductor) end to end on fakes. A temporary git repository stands in for ABP, scripted
functions stand in for the local model and the worker, and the integration suite is real pytest on that repository."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bot import coordination
from bot.conductor import integrator, llm, planner, reviewer, run, store
from bot.coordination import LocalBoard

CLI = '''import argparse


def build():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--limit", type=int, default=5)
    return p
'''
PAGE = '<html><body>\n<section id="status-root">\n<h2>Status</h2>\n</section>\n<section id="queue-root">queue</section>\n</body></html>\n'
TEST_CLI = "from cli import build\n\n\ndef test_build():\n    assert build() is not None\n"


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                           *args], check=check, capture_output=True, text=True).stdout


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "cli.py", CLI)
    write(root, "web/index.html", PAGE)
    write(root, "tests/test_cli.py", TEST_CLI)
    write(root, "pytest.ini", "[pytest]\npythonpath = .\n")
    write(root, "bot/hotreload.py", 'DENYLIST = {"bot.old"}\n')
    write(root, "bot/old.py", "X = 1\n")
    write(root, "notes.txt", "keep me\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def branch(repo, name, files, delete=(), start="main"):
    """Commit `files` (rel -> text) and deletions on a new branch from `start`; leave the repo on main."""
    git(repo, "checkout", "-q", "-b", name, start)
    for rel, text in files.items():
        write(repo, rel, text)
    for rel in delete:
        (Path(repo) / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "checkout", "-q", "main")
    return name


def rules(rep, rule):
    return [f for f in rep.findings if f.rule == rule]


@pytest.fixture(autouse=True)
def _clean():
    llm.set_completer(None)
    yield
    llm.set_completer(None)
    coordination.set_board(None)


def run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- reviewer
def test_reviewer_rejects_a_removed_cli_flag_and_dashboard_section_with_the_exact_lines(repo):
    branch(repo, "bad", {"cli.py": CLI.replace('    s.add_argument("--verbose", action="store_true")\n', ""),
                         "web/index.html": PAGE.replace('<section id="status-root">\n<h2>Status</h2>\n</section>\n', ""),
                         "tests/test_cli.py": TEST_CLI + "\n\ndef test_more():\n    assert True\n"})
    rep = reviewer.review(repo, "main", "bad", public=False, run_ruff=False)
    assert rep.verdict == "reject" and rep.checks["no_feature_removed"] == "fail"
    flag, section = [(f.kind, f.token, f.file, f.line, f.text.strip()) for f in rules(rep, "no_feature_removed")][:2]
    assert flag == ("CLI flag", "--verbose", "cli.py", 8, 's.add_argument("--verbose", action="store_true")')
    assert section == ("dashboard section", "status-root", "web/index.html", 2, '<section id="status-root">')
    kinds = {f.kind for f in rules(rep, "no_feature_removed")}
    assert "dashboard page heading" in kinds and not any(f.token == "--limit" for f in rep.findings)
    text = rep.to_text()
    assert "cli.py:8" in text and "--verbose" in text and "REJECT" in text
    assert json.loads(json.dumps(rep.to_dict()))["findings"][0]["rule"] == "no_feature_removed"


def test_reviewer_accepts_a_clean_diff_and_a_moved_flag(repo):
    branch(repo, "good", {"cli.py": CLI + "\n\ndef extra():\n    return 1\n",
                          "tests/test_extra.py": "from cli import extra\n\n\ndef test_extra():\n    assert extra() == 1\n"})
    rep = reviewer.review(repo, "main", "good", public=False)
    assert rep.verdict == "accept" and not rep.findings, rep.to_text()
    assert rep.checks["ruff"] in ("pass", "skipped: ruff is not installed")
    # A flag that is moved, not dropped, is kept.
    moved = CLI.replace('    s.add_argument("--verbose", action="store_true")\n', "") + \
        'def more(s):\n    s.add_argument("--verbose", action="store_true")\n'
    branch(repo, "moved", {"cli.py": moved, "tests/test_cli.py": TEST_CLI + "\n# touched\n"})
    assert reviewer.review(repo, "main", "moved", public=False, run_ruff=False).accepted


def test_reviewer_catches_routes_mcp_tools_config_enums_and_documented_functions(repo):
    write(repo, "bot/api.py", '@app.get("/api/x")\nasync def x():\n    return 1\n\n\n@app.post("/api/y")\nasync def y():\n    return 2\n')
    write(repo, "bot/tools.py", '@mcp.tool()\nasync def my_tool():\n    return 1\n\n\ndef public_fn():\n    return 1\n\n\nclass Color(Enum):\n    RED = 1\n    BLUE = 2\n')
    write(repo, "config/app.yaml", "alpha: 1\nbeta:\n  gamma: 2\n")
    write(repo, "docs/guide.md", "Call `public_fn` to do the thing.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "more base")
    branch(repo, "strip", {"bot/api.py": '@app.get("/api/x")\nasync def x():\n    return 1\n',
                           "bot/tools.py": "class Color(Enum):\n    RED = 1\n",
                           "config/app.yaml": "alpha: 1\n", "tests/test_cli.py": TEST_CLI + "\n# t\n"})
    rep = reviewer.review(repo, "main", "strip", public=False, run_ruff=False)
    got = {(f.kind, f.token) for f in rules(rep, "no_feature_removed")}
    assert ("route", "POST /api/y") in got and ("MCP tool", "my_tool") in got
    assert ("config key", "beta") in got and ("config key", "gamma") in got
    assert ("enum variant", "Color.BLUE") in got
    assert ("public function", "public_fn") in got
    assert ("route", "GET /api/x") not in got


def test_reviewer_other_standing_rules(repo):
    # a deletion, a machine path, an unclassified bot module, code without a test, a ruff error
    drive = "C" + ":/" + "Users" + "/someone_real/proj"
    branch(repo, "rules", {"bot/newmod.py": f'import os\nPATH = "{drive}"\n'}, delete=["notes.txt"])
    rep = reviewer.review(repo, "main", "rules")
    assert rep.verdict == "reject"
    assert [f.file for f in rules(rep, "no_deletions")] == ["notes.txt"]
    assert any("someone_real" in f.text or "drive" in f.message or "profile" in f.message for f in rules(rep, "leak_guard"))
    assert rules(rep, "hotreload")[0].token == "bot.newmod"
    assert rep.checks["tests"] == "fail"
    if "skipped" not in rep.checks["ruff"]:
        assert any("os" in f.message for f in rules(rep, "ruff")) and rules(rep, "ruff")[0].file == "bot/newmod.py"
    # told the file may go, and the leak rules off for a private repo: the deletion and the leak stop counting
    ok = reviewer.review(repo, "main", "rules", allow_delete=["notes.txt"], public=False, run_ruff=False)
    assert not rules(ok, "no_deletions") and ok.checks["leak_guard"].startswith("skipped")
    # a file the plan's earlier task created may be deleted by a later one
    assert not rules(reviewer.review(repo, "main", "rules", created_files=["notes.txt"], public=False, run_ruff=False), "no_deletions")


def test_model_review_is_a_separate_local_call_with_a_capped_prompt(repo):
    branch(repo, "m", {"cli.py": CLI + "\nZ = 1\n", "tests/test_cli.py": TEST_CLI + "\n# t\n"})
    rep = reviewer.review(repo, "main", "m", public=False, run_ruff=False)
    seen = {}

    async def reject(prompt, **kw):
        seen.update(prompt=prompt, kw=kw)
        return json.dumps({"verdict": "reject", "concerns": ["cli.py: Z is unused"]}), "modelmistress/reviewer"

    run_async(reviewer.model_review(rep, repo, goal="g", complete=reject, avoid="modelmistress/planner", diff_cap=600))
    assert rep.verdict == "reject" and rep.model == "modelmistress/reviewer" and rules(rep, "model")[0].message == "cli.py: Z is unused"
    assert len(seen["prompt"]) < 2200 and seen["kw"]["avoid"] == "modelmistress/planner"

    async def broken(prompt, **kw):
        raise RuntimeError("down")

    rep2 = reviewer.review(repo, "main", "m", public=False, run_ruff=False)
    run_async(reviewer.model_review(rep2, repo, complete=broken))
    assert rep2.verdict == "accept" and rep2.checks["model"].startswith("skipped")


def test_the_conductor_only_ever_asks_local_models(monkeypatch):
    monkeypatch.setattr("bot.capacity.routing.local_candidates", lambda: ["modelmistress/dusty", "ollama/qwen"])
    assert llm.candidates() == ["modelmistress/dusty", "ollama/qwen"]
    assert llm.candidates("", avoid="modelmistress/dusty")[-1] == "modelmistress/dusty"
    with pytest.raises(llm.NoLocalModel):
        llm.candidates("anthropic/claude-x")


# ----------------------------------------------------------------- planner
def good_plan():
    return json.dumps({"tasks": [
        {"id": "Add Flag", "title": "Add a --json flag", "files": ["cli.py", "../escape", "C" + ":/abs"], "depends_on": [],
         "proof": {"command": "python -m pytest tests/test_cli.py -q", "description": "cli tests"}, "capacity": "local"},
        {"id": "docs", "title": "Document it", "depends_on": ["add-flag", "ghost"], "proof": "python -m pytest tests -q", "capacity": "weird"}]})


def test_the_planner_repairs_a_bad_answer_and_stores_a_stable_plan():
    answers = ['not json at all', json.dumps({"tasks": [{"id": "a", "title": "A"}]}), "```json\n" + good_plan() + "\n```"]
    prompts = []

    async def fake(prompt, **kw):
        prompts.append(prompt)
        return answers.pop(0), "modelmistress/dusty"

    llm.set_completer(fake)
    plan = run_async(planner.make_plan("Add a --json flag to status", repo="abp", worktree_root="/wt"))
    assert plan.planner == "model:modelmistress/dusty" and plan.repaired == 2
    assert "not usable" in prompts[1] and "no proof" in prompts[2]
    a, b = plan.tasks
    assert (a.id, b.id, b.deps) == ("add-flag", "docs", ["add-flag"])     # slugged ids, the unknown dep dropped
    assert a.files == ["cli.py"] and b.capacity == "" and a.capacity == "local"
    assert a.key == f"conductor:{plan.id}:add-flag"
    assert a.scope["resources"] == [f"worktree:/wt/{plan.id[:8]}-add-flag", f"branch:abp:{a.branch}", "path:abp:cli.py"]
    again = run_async(planner.make_plan("add a --json flag  to status", repo="abp"))      # same goal -> the stored plan, no model call
    assert again.to_dict() == plan.to_dict()


def test_the_planner_falls_back_without_a_model_and_rejects_cycles():
    async def none(prompt, **kw):
        raise llm.NoLocalModel("none")

    llm.set_completer(none)
    plan = run_async(planner.make_plan("- first thing\n- second thing", repo="abp"))
    assert plan.planner == "fallback" and [t.deps for t in plan.tasks] == [[], [plan.tasks[0].id]]
    assert all(t.proof["command"] for t in plan.tasks)
    _, errors = planner.validate({"tasks": [{"id": "a", "title": "A", "depends_on": ["b"], "proof": "python -m pytest tests -q"},
                                            {"id": "b", "title": "B", "depends_on": ["a"], "proof": "python -m pytest tests -q"}]})
    assert errors and "cycle" in errors[0]
    with pytest.raises(planner.PlanError):
        run_async(planner.make_plan("   "))


def test_a_goal_can_be_a_roadmap_row_or_a_todo_line(tmp_path):
    (tmp_path / "TODO.md").write_text("# T\n- [ ] ship the thing now\n| LF9 | do it | 2 days |\n|---|---|---|\n", encoding="utf-8")
    assert planner.goal_from("TODO.md:2", tmp_path) == ("ship the thing now", "TODO.md:2")
    assert planner.goal_from("TODO.md:3", tmp_path)[0] == "LF9 - do it - 2 days"
    assert planner.goal_from("plain words", tmp_path) == ("plain words", "")


# -------------------------------------------------------------- integrator
def suite_repo(repo, flaky_mark, with_failure=False):
    files = {"tests/test_flaky.py": ("import os\nimport pathlib\n\n\ndef test_flaky():\n    m = pathlib.Path(os.environ['FLAKE_MARK'])\n"
                                     "    if not m.exists():\n        m.write_text('x')\n        assert False, 'first run'\n")}
    if with_failure:
        files["tests/test_broken.py"] = "def test_broken():\n    assert 1 == 2\n"
    return files


def test_the_integrator_tells_a_flake_from_a_failure_and_queues_only_green(repo, tmp_path, monkeypatch):
    mark = tmp_path / "flake.mark"
    monkeypatch.setenv("FLAKE_MARK", str(mark))
    ok = branch(repo, "feat/ok", suite_repo(repo, mark))
    other = branch(repo, "feat/other", {"extra.py": "Y = 2\n"})
    integ = integrator.Integrator(repo, base="main", workdir=tmp_path / "wt")
    green = integ.integrate([ok, other], plan_id="p1", tasks=["a", "b"])
    assert green.status == "green" and green.branch == "integ/agents-1" and green.merged == [ok, other]
    assert [t.split("::")[-1] for t in green.flaky] == ["test_flaky"] and green.failed == []
    assert "passed" in green.tests_run and Path(green.log).is_file()
    q = store.list_queue()
    assert [(e["branch"], e["status"], e["head_sha"] == green.head_sha) for e in q] == [("integ/agents-1", "awaiting_approval", True)]
    assert len(q[0]["flaky"]) == 1 and q[0]["id"] == green.queue_id
    assert git(repo, "rev-parse", "main").strip() != green.head_sha            # main was not touched

    mark.unlink()
    bad = branch(repo, "feat/bad", suite_repo(repo, mark, with_failure=True))
    red = integ.integrate([bad], plan_id="p2")
    assert red.status == "red" and red.branch == "integ/agents-2"
    assert [t.split("::")[-1] for t in red.failed] == ["test_broken"] and [t.split("::")[-1] for t in red.flaky] == ["test_flaky"]
    assert len(store.list_queue()) == 1                                           # a red result is never queued
    assert {i["status"] for i in store.list_integrations()} == {"green", "red"}


def test_the_integrator_leaves_out_a_branch_that_conflicts(repo, tmp_path):
    a = branch(repo, "feat/a", {"notes.txt": "from a\n"})
    b = branch(repo, "feat/b", {"notes.txt": "from b\n"})
    res = integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=[sys.executable, "-c", "pass"]).integrate([a, b])
    assert res.status == "green" and res.merged == [a] and res.conflicts[0]["branch"] == b and "notes.txt" in res.conflicts[0]["files"]
    assert "left out" in res.note


def test_the_push_queue_waits_for_an_approval_and_never_pushes(repo, tmp_path):
    a = branch(repo, "feat/a", {"extra.py": "Z = 1\n"})
    integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=[sys.executable, "-c", "pass"]).integrate([a])
    entry = store.list_queue()[0]
    with pytest.raises(ValueError):
        store.decide(entry["id"], "approved", "")                                  # who decides is required
    out = run.approve(entry["id"], "owner", "ship it")
    assert out["status"] == "approved" and out["decided_by"] == "owner" and out["push_command"] == f"git push origin {entry['branch']}"
    with pytest.raises(ValueError):
        run.approve(entry["id"], "owner")                                          # decided once
    with pytest.raises(KeyError):
        run.approve("nope", "owner")
    assert not git(repo, "remote").strip()                                         # no remote, nothing pushed


# ------------------------------------------------------- end to end on fakes
def scripted_drive(plan_calls):
    """A fake local agent: its first try at 'add-flag' drops the --verbose flag and the status page section (the review must
    refuse it); told why, the second try adds the flag properly. Other tasks add a function and a test."""
    async def drive(prompt, wt):
        wt = Path(wt)
        plan_calls.append(prompt)
        if prompt.startswith("Task: Add a --json flag"):
            cli = (wt / "cli.py").read_text(encoding="utf-8")
            if "REJECTED" not in prompt:
                cli = cli.replace('    s.add_argument("--verbose", action="store_true")\n', "")
                (wt / "web/index.html").write_text(PAGE.replace('<section id="status-root">\n<h2>Status</h2>\n</section>\n', ""),
                                                   encoding="utf-8", newline="\n")
            else:                                               # put back what the first try dropped, then add the flag
                (wt / "web/index.html").write_text(PAGE, encoding="utf-8", newline="\n")
                cli = CLI + '\n\ndef json_flag(s):\n    s.add_argument("--json", action="store_true")\n'
            (wt / "cli.py").write_text(cli, encoding="utf-8", newline="\n")
            (wt / "tests/test_json.py").write_text("from cli import json_flag\n\n\ndef test_json():\n    assert json_flag\n", encoding="utf-8", newline="\n")
            return "added the flag"
        (wt / "docs_note.py").write_text('NOTE = "documented"\n', encoding="utf-8", newline="\n")
        (wt / "tests/test_note.py").write_text("from docs_note import NOTE\n\n\ndef test_note():\n    assert NOTE\n", encoding="utf-8", newline="\n")
        return "documented"
    return drive


def test_goal_to_plan_to_reviewed_diffs_to_a_queued_integration_branch(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKE_MARK", str(tmp_path / "m"))
    calls: list[str] = []

    async def fake_model(prompt, **kw):
        return json.dumps({"tasks": [
            {"id": "add-flag", "title": "Add a --json flag", "files": ["cli.py"], "proof": {"command": "python -m pytest tests/test_json.py -q"}},
            {"id": "docs", "title": "Document it", "depends_on": ["add-flag"], "proof": {"command": "python -m pytest tests/test_note.py -q"}}]}), \
            "modelmistress/dusty"

    llm.set_completer(fake_model)
    board = LocalBoard(tmp_path / "board.db")
    coordination.set_board(board)
    try:
        worker = run.AgentLoopWorker(repo, drive=scripted_drive(calls))
        cond = run.Conductor(repo, worker, board=board, workdir=tmp_path / "wt", suite=[sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rf"])
        cond.cfg = {**cond.cfg, "check_ruff": True, "public_repo": True}
        plan = run_async(cond.plan("Add a --json flag to the status command and document it"))
        assert plan.planner == "model:modelmistress/dusty" and plan.tasks[1].deps == ["add-flag"]
        report = run_async(cond.run(plan, integrate=True))

        # the first try was refused with the exact reasons, the second accepted; the dependent only started after
        first = store.list_reviews(plan.id, plan.tasks[0].key)
        assert [r["verdict"] for r in reversed(first)] == ["reject", "accept"], json.dumps(first[0]["report"]["findings"])
        reasons = {(f["kind"], f["token"]) for f in first[-1]["report"]["findings"]}
        assert ("CLI flag", "--verbose") in reasons and ("dashboard section", "status-root") in reasons
        rejected_calls = [c for c in calls if "REJECTED" in c]
        assert len(rejected_calls) == 1 and "--verbose" in rejected_calls[0]
        assert report.tasks["add-flag"]["rejections"] == 1 and report.tasks["docs"]["status"] == "success"
        assert not report.escalated and not report.not_run and len(report.accepted) == 2

        # the board carried the plan: keys, deps and scopes
        tasks = {t["title"]: t for t in board.list_tasks(limit=50)}
        flag = next(t for t in tasks.values() if "--json" in t["title"])
        assert flag["key"] == plan.tasks[0].key and flag["state"] == "done"
        assert flag["scope"]["resources"] == plan.tasks[0].scope["resources"]
        docs = next(t for t in tasks.values() if "Document" in t["title"])
        assert docs["deps"] == [flag["id"]]

        integ = report.integration
        assert integ["status"] == "green" and integ["merged"] == report.accepted
        queue = store.list_queue("awaiting_approval")
        assert len(queue) == 1 and queue[0]["branch"] == integ["branch"] and queue[0]["plan_id"] == plan.id
        assert "--verbose" in git(repo, "show", f"{integ['branch']}:cli.py")          # the flag the first try dropped is still there
        assert run.status()["queue"]["awaiting_approval"][0]["id"] == queue[0]["id"]
    finally:
        board.close()


def test_two_rejections_escalate_and_block_the_dependents(repo, tmp_path):
    async def model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "bad", "title": "Bad change", "proof": "python -m pytest tests -q"},
                                     {"id": "after", "title": "After it", "depends_on": ["bad"], "proof": "python -m pytest tests -q"}]}), "m/x"

    async def drive(prompt, wt):
        cli = Path(wt, "cli.py")
        cli.write_text(cli.read_text(encoding="utf-8").replace('    s.add_argument("--verbose", action="store_true")\n', ""), encoding="utf-8", newline="\n")
        Path(wt, "tests/test_cli.py").write_text(TEST_CLI + "\n# touched\n", encoding="utf-8", newline="\n")
        return "oops"

    llm.set_completer(model)
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = run.Conductor(repo, run.AgentLoopWorker(repo, drive=drive), board=board, workdir=tmp_path / "wt")
        cond.cfg = {**cond.cfg, "check_ruff": False, "public_repo": False}
        plan = run_async(cond.plan("do a bad thing, then more"))
        report = run_async(cond.run(plan, integrate=True))
        assert report.escalated == ["bad"] and report.not_run == ["after"] and report.integration is None
        assert [r["verdict"] for r in store.list_reviews(plan.id)] == ["reject", "reject"]
        esc = store.list_escalations()
        assert len(esc) == 1 and esc[0]["task_key"] == plan.tasks[0].key and "2 review rejections" in esc[0]["reason"]
        assert "--verbose" in esc[0]["reason"]
        task = next(t for t in board.list_tasks(limit=20) if t["key"] == plan.tasks[0].key)
        assert task["state"] == "blocked" and task["note"].startswith("ESCALATED")
        assert store.list_queue() == []
    finally:
        board.close()


# --------------------------------------------------------- API, CLI, MCP
@pytest.fixture
def client(monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    return TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}


def test_the_api_cli_and_mcp_surfaces(client, repo, tmp_path, capsys):
    c, h = client
    assert c.get("/api/conductor/status").status_code == 401
    assert c.post("/api/conductor/queue/x/approve", json={"by": "o"}).status_code == 401

    async def none(prompt, **kw):
        raise llm.NoLocalModel("none")

    llm.set_completer(none)
    plan = c.post("/api/conductor/plan", headers=h, json={"goal": "tidy the thing"}).json()
    assert plan["planner"] == "fallback" and c.get(f"/api/conductor/plans/{plan['id']}", headers=h).json()["goal"] == "tidy the thing"
    assert c.post("/api/conductor/plan", headers=h, json={"goal": " "}).status_code == 400

    branch(repo, "bad", {"cli.py": CLI.replace('    s.add_argument("--verbose", action="store_true")\n', ""),
                         "tests/test_cli.py": TEST_CLI + "\n# t\n"})
    rev = c.post("/api/conductor/review", headers=h, json={"repo": str(repo), "head": "bad", "base": "main"}).json()
    assert rev["verdict"] == "reject" and "--verbose" in rev["text"]

    a = branch(repo, "feat/a", {"extra.py": "Z = 1\n"})
    integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=[sys.executable, "-c", "pass"]).integrate([a])
    q = c.get("/api/conductor/queue", headers=h, params={"status": "awaiting_approval"}).json()["queue"]
    assert len(q) == 1
    assert c.get("/api/conductor/status", headers=h).json()["queue"]["awaiting_approval"][0]["id"] == q[0]["id"]
    assert c.post(f"/api/conductor/queue/{q[0]['id']}/approve", headers=h, json={"by": ""}).status_code == 400
    assert c.post("/api/conductor/queue/zzz/approve", headers=h, json={"by": "o"}).status_code == 404

    from abp_cli import __main__ as cli

    class Fake:
        async def _request(self, method, path, params=None, json=None):
            r = c.request(method, path, headers=h, params=params, json=json)
            r.raise_for_status()
            return r.json()

    class Args:
        json = False
        conductor_cmd = "status"

    assert asyncio.run(cli._conductor(Args(), Fake())) == 0
    out = capsys.readouterr().out
    assert "push queue: 1 awaiting approval" in out and "local only" in out
    a2 = Args()
    a2.conductor_cmd, a2.queue_id, a2.by, a2.note = "approve", q[0]["id"], "claude", "looks fine"
    assert asyncio.run(cli._conductor(a2, Fake())) == 0
    assert "nothing was pushed" in capsys.readouterr().out
    assert c.get("/api/conductor/queue", headers=h).json()["queue"][0]["status"] == "approved"
    parsed = cli._parser().parse_args(["conductor", "approve", "abc", "--by", "owner"])
    assert parsed.conductor_cmd == "approve" and parsed.by == "owner"

    from bot import mcp_server

    seen = []

    async def fake(method, path, timeout=15.0, **kw):
        seen.append((method, path))
        return {}

    mcp_server._request, orig = fake, mcp_server._request
    try:
        async def go():
            await mcp_server.conductor_status()
            await mcp_server.conductor_plan("g")
            await mcp_server.conductor_review("r", "h")
            await mcp_server.conductor_queue("approved")
            await mcp_server.conductor_approve("q1", "claude")
            await mcp_server.conductor_approve("q2", "claude", reject=True)

        asyncio.run(go())
    finally:
        mcp_server._request = orig
    assert seen == [("GET", "/api/conductor/status"), ("POST", "/api/conductor/plan"), ("POST", "/api/conductor/review"),
                    ("GET", "/api/conductor/queue"), ("POST", "/api/conductor/queue/q1/approve"), ("POST", "/api/conductor/queue/q2/reject")]


def test_follow_the_capacity_state_cloud_hinted_tasks_wait_in_local_only(repo, tmp_path):
    from bot.capacity import follow

    assert follow.PREFIX + "cloud" not in follow.worker_capabilities(state="local-only")
    assert follow.PREFIX + "cloud" in follow.worker_capabilities(state="cloud-led")


# ----------------------------------------------------- proofs run confined
@pytest.mark.parametrize("cmd", [
    "cmd /c del important.txt", 'python -c "import os"', "powershell -Command Get-Process", "pytest ../outside",
    "python -m pytest tests/test_cli.py; rm -rf x", "python -m pytest tests/test_cli.py && echo hi", "python -m pytest -p evil tests",
    "python -m pytest tests/test_cli.py --rootdir=/", "python -m pytest /etc/passwd", "python -m pytest tests/../../x.py",
    "ruff check --fix .", "ruff check ../x", "", "echo hi"])
def test_a_proof_that_is_not_a_test_run_is_refused_and_never_started(cmd, repo, monkeypatch):
    from bot.agent_runtime import sandbox, shell

    started = []
    monkeypatch.setattr(sandbox, "start", lambda *a, **k: started.append(a))
    monkeypatch.setattr(shell, "_run_fresh", lambda *a, **k: started.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: started.append(a))
    finding = run_async(run.run_proof({"proof": {"command": cmd}}, str(repo))) if cmd else None
    assert planner.validate({"tasks": [{"id": "a", "title": "A", "proof": cmd or "x"}]})[1]      # refused at planning time too
    if cmd:
        assert finding is not None and finding.message.startswith("proof not allowed:")
    assert started == []


def test_an_allowed_proof_runs_through_the_sandbox_spawn(repo, monkeypatch):
    from bot.agent_runtime import sandbox

    seen = []

    class Proc:
        returncode = 0

        def __init__(self, ok):
            self.returncode = 0 if ok else 1
            self._data = [b"1 passed in 0.01s\n" if ok else b"FAILED tests/test_cli.py::test_build\n1 failed in 0.01s\n", b""]
            self.stdout = self

        async def read(self, n):
            return self._data.pop(0)

        async def wait(self):
            return self.returncode

    results = [True, False]

    async def fake_start(command, cwd, workspace):
        seen.append((command, Path(cwd), Path(workspace)))
        return Proc(results.pop(0))

    monkeypatch.setattr(sandbox, "start", fake_start)
    spec = {"proof": {"command": "python -m pytest tests/test_cli.py -q"}}
    assert run_async(run.run_proof(spec, str(repo))) is None
    bad = run_async(run.run_proof(spec, str(repo)))
    assert bad.rule == "proof" and "failed" in bad.message
    assert [(c.startswith("python -m pytest -q -x"), cwd == repo.resolve(), ws == repo.resolve()) for c, cwd, ws in seen] == [(True, True, True)] * 2
    assert "tests/test_cli.py" in seen[0][0]
