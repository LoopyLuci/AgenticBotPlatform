"""Fixes from the first LF5 measurement run (a real conductor task on a local model, 2026-10-19):

A. `abp conductor run --model` reached only the worker; the planner fell back to the router's default refs.
B. The fallback plan's proof was the whole suite (never passes in a no-network sandbox), and a proof ran with whatever `python`
   the PATH found first instead of the worker's interpreter and sandbox grants.
C. LF5's factory token accounting read only agent traces; the conductor's headless worker turns write none.
D. `abp conductor run` (in-process) demanded a dashboard token it never uses.

And from the third run (the first accepted task; the other failed on model slips, not the environment):
- an unused import (ruff F401, in every run) cost a review round: ruff's safe fixes are now applied before review;
- the worker wrote its tests into a different file than its proof runs: the prompt now names that file, the planner is told to;
- workers never used the code-navigation tools: the prompt now says how to read code with them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.conductor as conductor_pkg
from bot.conductor import llm, planner, proof, run, store
from bot.manager import digest


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "mod.py", "X = 1\n")
    write(root, "tests/test_mod.py", "from mod import X\n\n\ndef test_x():\n    assert X == 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture(autouse=True)
def _clean():
    llm.set_completer(None)
    yield
    llm.set_completer(None)


def one_task_plan():
    return json.dumps({"tasks": [{"id": "a", "title": "A", "proof": "python -m pytest tests/test_mod.py -q"}]})


# ---- A: --model plans as well as works -----------------------------------------------------------------------------
def test_the_conductor_model_is_the_planner_model(repo):
    asked = []

    async def model(prompt, **kw):
        asked.append(kw.get("model"))
        return one_task_plan(), kw.get("model") or "m/x"

    llm.set_completer(model)
    plan = asyncio.run(run.Conductor(repo, None, model="modelmistress/vendor/coder-GGUF:Q4").plan("do the thing", replan=True))
    assert asked == ["modelmistress/vendor/coder-GGUF:Q4"]
    assert plan.planner == "model:modelmistress/vendor/coder-GGUF:Q4"


# ---- D (and A's CLI wiring): conductor run needs no dashboard token ------------------------------------------------
def test_conductor_run_needs_no_dashboard_and_passes_model_to_the_planner(monkeypatch, tmp_path, capsys):
    from abp_cli import __main__ as cli

    monkeypatch.setattr(cli, "_client", lambda args: pytest.fail("conductor run must not need the dashboard"))
    made = {}

    class Worker:
        def __init__(self, repo, **kw):
            made["worker"] = kw

    class Cond:
        def __init__(self, repo, worker, **kw):
            made["conductor"] = kw

        async def plan(self, goal):
            return SimpleNamespace(id="p1", planner="model:x", tasks=[])

        async def run(self, plan, integrate=False):
            return run.RunReport(plan_id="p1")

    monkeypatch.setattr(run, "AgentLoopWorker", Worker)
    monkeypatch.setattr(run, "Conductor", Cond)
    args = cli._parser().parse_args(["conductor", "run", "a goal", "--repo", str(tmp_path), "--model", "modelmistress/m"])
    assert asyncio.run(cli._run(args)) == 0
    assert made["worker"]["model"] == "modelmistress/m" and made["conductor"]["model"] == "modelmistress/m"
    assert "plan p1" in capsys.readouterr().out


def test_only_the_in_process_conductor_commands_skip_the_dashboard():
    from abp_cli import __main__ as cli

    assert set(cli.IN_PROCESS_CONDUCTOR) == {"run", "unblock", "review"}


# ---- B: the fallback proof and the proof's environment ---------------------------------------------------------------
def test_the_fallback_plan_proves_each_task_with_its_own_tests():
    tasks = planner.fallback_tasks("first thing; second thing")
    assert [t["proof"]["command"] for t in tasks] == [proof.CHANGED_TESTS] * 2
    assert proof.check(proof.CHANGED_TESTS) is None
    assert "tests you add or change" in proof.describe(proof.CHANGED_TESTS)
    assert proof.describe("python -m pytest tests/test_mod.py") == "python -m pytest tests/test_mod.py"


def test_changed_tests_resolve_to_the_test_files_the_branch_touched(repo):
    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(proof.ProofRefused, match="no test file"):
        proof.resolve(proof.CHANGED_TESTS, repo, base)
    write(repo, "mod.py", "X = 2\n")
    write(repo, "tests/test_new.py", "def test_new():\n    assert True\n")
    write(repo, "tests/helpers.py", "Y = 1\n")                          # not a test file
    write(repo, "tests/test_mod.py", "from mod import X\n\n\ndef test_x():\n    assert X == 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    assert proof.resolve(proof.CHANGED_TESTS, repo, base) == "python -m pytest tests/test_mod.py tests/test_new.py"
    assert proof.resolve("ruff check mod.py", repo, base) == "ruff check mod.py"     # any other proof is left alone


def test_a_proof_runs_inside_the_worker_environment_holding_the_agent_lock(repo, monkeypatch):
    seen = []

    async def fake_execute(kind, targets, k, ws, timeout_s):
        seen.append((os.environ.get("ABP_TEST_PROOF_ENV"), proof.ENV_LOCK.locked(), kind, targets))
        return True, "PASSED"

    @contextlib.contextmanager
    def env():
        os.environ["ABP_TEST_PROOF_ENV"] = "worker"
        try:
            yield
        finally:
            os.environ.pop("ABP_TEST_PROOF_ENV", None)

    monkeypatch.setattr(proof, "_execute", fake_execute)
    ok, _ = asyncio.run(proof.run("python -m pytest tests/test_mod.py", repo, cache=SimpleNamespace(enabled=False), env=env))
    assert ok and seen == [("worker", True, "pytest", ["tests/test_mod.py"])]
    assert "ABP_TEST_PROOF_ENV" not in os.environ and not proof.ENV_LOCK.locked()


def test_agent_turns_and_proofs_share_one_lock():
    assert run.AgentLoopWorker._agent_lock is proof.ENV_LOCK


def test_the_proof_environment_is_the_workers(repo):
    async def scripted(spec, feedback):
        return {}

    from bot.config import config

    assert run.Conductor(repo, scripted)._proof_env() is None              # a scripted test worker: the proof runs as configured
    for shell in ("none", "sandbox"):
        env = run.Conductor(repo, SimpleNamespace(shell=shell))._proof_env()
        before = os.environ.get("PATH", "")
        with env():
            assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
            # the configured sandbox, not the sandbox shell's AppContainer overlay
            assert "sandbox" not in (config._data.get("native_agent") or {}) or \
                (config._data["native_agent"]["sandbox"] or {}).get("network") != "none"
        assert os.environ.get("PATH", "") == before


# ---- C: the conductor's tokens reach LF5's accounting --------------------------------------------------------------
def test_conductor_worker_tokens_reach_the_lf5_accounting():
    store.add_tool_calls("p1", "conductor:p1:a", 1, [{"tool": "read_file"}], "tokens=261008", tokens=261008)
    store.add_tool_calls("p1", "conductor:p1:a", 2, [], "tokens=131653", tokens=131653)
    assert store.worker_tokens(0) == {"attempts": 2, "tokens": 392661}
    assert [r["tokens"] for r in store.list_tool_calls("p1")] == [261008, 131653]
    f = digest.factory_tokens(0)
    assert f["conductor_tokens"] == 392661 and f["conductor_attempts"] == 2
    assert f["tokens"] >= 392661 and f["local_tokens"] >= 392661 and "conductor" in f["source"]
    assert digest.build(since=0, fresh=True)["accounting"]["factory_tokens_since_digest"] >= 392661


def test_an_older_conductor_db_gains_the_tokens_column():
    path = store.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE toolcalls (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, task_key TEXT NOT NULL, "
                 "attempt INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, summary TEXT NOT NULL DEFAULT '', data TEXT NOT NULL)")
    conn.execute("INSERT INTO toolcalls(plan_id, task_key, attempt, created, summary, data) VALUES ('old','k',1,1.0,'','[]')")
    conn.commit()
    conn.close()
    store._migrated.discard(str(path))
    store.add_tool_calls("new", "k", 1, [], tokens=5)
    assert store.worker_tokens(0) == {"attempts": 2, "tokens": 5}


# ---- run 3: worker slips -------------------------------------------------------------------------------------------
def test_ruff_safe_fixes_are_committed_on_the_branch_before_review(repo):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "tests/test_new.py", "import os\nimport pytest\n\n\ndef test_new():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker")
    assert run.autofix_lint(repo, base) == ["tests/test_new.py"]
    assert git(repo, "log", "-1", "--format=%s") == run.AUTOFIX_COMMIT
    assert "import" not in (repo / "tests/test_new.py").read_text(encoding="utf-8")
    assert git(repo, "status", "--porcelain") == ""
    assert run.autofix_lint(repo, base) == []                                   # nothing left to fix: no empty commit
    assert git(repo, "log", "-1", "--format=%s") == run.AUTOFIX_COMMIT and git(repo, "rev-list", "--count", f"{base}..HEAD") == "2"


def test_autofix_only_touches_the_branch_s_python_and_never_unsafe_fixes(repo):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "mod.py", "X = 1\n\n\ndef f(x):\n    y = 2\n    return x\n")          # F841 needs --unsafe-fixes: left alone
    write(repo, "notes.txt", "import os\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker")
    assert run.autofix_lint(repo, base) == []
    assert "y = 2" in (repo / "mod.py").read_text(encoding="utf-8")
    assert conductor_pkg.DEFAULTS["autofix_lint"] is True


def test_the_proof_s_test_file_is_named_to_the_worker():
    assert proof.target_files("python -m pytest tests/test_a.py::test_x tests/test_b.py tests/test_a.py -q") == \
        ["tests/test_a.py", "tests/test_b.py"]
    assert proof.target_files(proof.CHANGED_TESTS) == [] and proof.target_files("ruff check mod.py") == []
    assert proof.target_files("rm -rf /") == []
    assert "proof runs exactly that file" in planner.SCHEMA_HELP


def test_the_worker_prompt_names_the_test_file_and_how_to_navigate(repo, tmp_path):
    prompts = []

    async def drive(prompt, wt):
        prompts.append(prompt)
        return "nothing to do"

    worker = run.AgentLoopWorker(repo, drive=drive, shell="none")
    spec = {"plan_id": "p", "goal": "g", "id": "t", "key": "conductor:p:t", "title": "Test X", "detail": "add a test",
            "files": ["tests/test_x.py"], "proof": {"command": "python -m pytest tests/test_x.py::test_x -q"},
            "branch": "feat/t", "worktree": str(tmp_path / "wt"), "base": "main", "after": []}
    asyncio.run(worker(spec, ""))
    assert "Put the tests in exactly tests/test_x.py" in prompts[0] and "outline(path)" in prompts[0]
    assert "no unused imports" in prompts[0]
