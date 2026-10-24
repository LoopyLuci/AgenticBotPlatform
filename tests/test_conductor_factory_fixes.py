"""Fixes from the first confined Factory run from Local Chat (2026-10-20; throwaway repo, goal: add slugify(text) to textutil.py with
tests, 3-task plan on a local coder model). It escalated after 2 rejections:

1. The proof ran with the live bundle's runtime venv, which had no pytest ("No module named pytest"). A proof now runs with the
   repository's own venv when it has pytest, else ABP's runtime, which ships pytest; a missing pytest escalates at once as an
   environment problem instead of being sent to the worker as its failure.
2. ruff I001 / RUF010 marked [*] still rejected: the task worktrees live inside another project, so the safe-fix step's ruff took
   that project's config while the review linted with the repository's own (`--isolated`). Both now lint the same copy; that copy
   also keeps CRLF files byte for byte (text-mode writing doubled the line ends: shifted lines and a phantom I001).
3. Told the proof failed for want of pytest, the worker wrote a test script (test_slugify.py) instead of the plan's
   tests/test_textutil.py. The prompt lists the proof's test file and asks for pytest tests; a proof naming a file that does not
   exist says so, and where the attempt put its tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import coordination
from bot.conductor import confine, llm, proof, reviewer, run, store
from bot.coordination import LocalBoard

ROOT = Path(__file__).resolve().parent.parent


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def write(root, rel, text, newline="\n"):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline=newline)


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    write(root, "textutil.py", "def upper(text):\n    return text.upper()\n")
    write(root, "tests/test_upper.py", "from textutil import upper\n\n\ndef test_upper():\n    assert upper('a') == 'A'\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture(autouse=True)
def _clean():
    llm.set_completer(None)
    yield
    llm.set_completer(None)


def fake_venv(repo: Path, name: str = ".venv", *, pytest_installed: bool = True, home: Path | None = None) -> Path:
    venv = repo / name
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    sp = venv / ("Lib/site-packages" if os.name == "nt" else "lib/python3.11/site-packages")
    sp.mkdir(parents=True)
    if pytest_installed:
        (sp / "pytest").mkdir()
        (sp / "pytest" / "__init__.py").write_text("", encoding="utf-8")
    if home:
        (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.11.9\n", encoding="utf-8")
    return exe


# ---- 1: the proof's Python -------------------------------------------------------------------------------------------
def test_the_proof_python_is_the_repository_venv_when_it_has_pytest(repo):
    assert confine.test_python(repo) == sys.executable                     # no venv: ABP's own runtime
    fake_venv(repo, "venv", pytest_installed=False)
    assert confine.test_python(repo) == sys.executable                     # a venv without pytest cannot run the proof either
    exe = fake_venv(repo, ".venv")
    assert confine.test_python(repo) == str(exe)


def test_the_sandbox_may_read_the_repository_venv_and_its_base_install(repo, tmp_path):
    base = tmp_path / "base-python"
    base.mkdir()
    exe = fake_venv(repo, home=base / ("" if os.name == "nt" else "bin"))
    paths = [os.path.normcase(p) for p in confine.sandbox_paths(str(exe))]
    assert os.path.normcase(str(repo / ".venv")) in paths and os.path.normcase(str(base)) in paths
    assert set(confine.sandbox_paths()) <= set(confine.sandbox_paths(str(exe)))   # ABP's own runtime stays readable (ruff proofs)
    assert len(paths) == len(set(paths))


def test_the_worker_and_its_proof_use_the_same_python(repo):
    exe = fake_venv(repo)
    worker = run.AgentLoopWorker(repo, shell="none", drive=lambda *a: None)
    assert worker.python == str(exe)
    env = run.Conductor(repo, worker)._proof_env()
    with env():
        assert os.environ["PATH"].split(os.pathsep)[0] == str(exe.parent)


def test_abp_s_runtime_ships_pytest():
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("pytest>=") for line in reqs)
    assert "\npytest==" in (ROOT / "requirements.lock").read_text(encoding="utf-8")
    from scripts import stage_bundle

    assert not {"pytest", "_pytest", "pluggy", "iniconfig"} & stage_bundle.DEV_ONLY_PACKAGES


def test_a_proof_without_pytest_is_an_environment_problem_not_the_worker_s(repo, monkeypatch):
    async def no_pytest(*a, **k):
        return False, "FAILED: no summary line (exit code 1)\nC:\\abp\\.venv\\Scripts\\python.exe: No module named pytest"

    monkeypatch.setattr(proof, "run", no_pytest)
    spec = {"proof": {"command": "python -m pytest tests/test_upper.py -q"}}
    bad = asyncio.run(run.run_proof(spec, str(repo)))
    assert bad.kind == run.ENVIRONMENT and "no pytest" in bad.message


def test_a_missing_pytest_escalates_at_once_without_a_second_attempt(repo, tmp_path, monkeypatch):
    async def model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "slug", "title": "Add slugify", "files": ["textutil.py", "tests/test_upper.py"],
                                      "proof": "python -m pytest tests/test_upper.py -q"}]}), "m/x"

    attempts = []

    async def drive(prompt, wt):
        attempts.append(prompt)
        write(wt, "textutil.py", "def upper(text):\n    return text.upper()\n\n\ndef slugify(text):\n    return text.lower()\n")
        write(wt, "tests/test_upper.py", "from textutil import slugify\n\n\ndef test_slugify():\n    assert slugify('A') == 'a'\n")
        return "done"

    async def no_pytest(*a, **k):
        return False, "python.exe: No module named pytest"

    monkeypatch.setattr(proof, "run", no_pytest)
    llm.set_completer(model)
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = run.Conductor(repo, run.AgentLoopWorker(repo, drive=drive), board=board, workdir=tmp_path / "wt")
        cond.cfg = {**cond.cfg, "check_ruff": False, "public_repo": False, "model_review": False}
        plan = asyncio.run(cond.plan("add slugify"))
        report = asyncio.run(cond.run(plan, integrate=False))
        assert report.escalated == ["slug"] and len(attempts) == 1
        esc = store.list_escalations()
        assert len(esc) == 1 and "cannot run here" in esc[0]["reason"] and "pytest" in esc[0]["reason"]
    finally:
        board.close()


# ---- 2: every safe fix the review would ask for ------------------------------------------------------------------------
UNSORTED = "import sys\nimport os\n\n\ndef show(x):\n    return f\"{repr(x)} {os.sep} {sys.argv}\"\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_autofix_fixes_what_the_review_flags_even_inside_another_project(tmp_path, newline):
    # The parent project's config selects only E: a ruff run in place would take it, fix nothing, and the review (which lints with
    # the repository's own config) would still reject. The repository asks for I and RUF.
    write(tmp_path, "pyproject.toml", '[tool.ruff.lint]\nselect = ["E"]\n')
    repo = make_repo(tmp_path / "parent" / "repo")
    write(repo, "pyproject.toml", '[tool.ruff.lint]\nselect = ["E", "F", "I", "RUF"]\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "test_slugify.py", UNSORTED, newline=newline)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker")
    found, state = reviewer.check_ruff(repo, "HEAD", reviewer.changes(repo, base, "HEAD"))
    assert state == "fail" and {f.message.split()[0] for f in found} == {"I001", "RUF010"}

    assert run.autofix_lint(repo, base) == ["test_slugify.py"]
    assert git(repo, "log", "-1", "--format=%s") == run.AUTOFIX_COMMIT and git(repo, "status", "--porcelain") == ""
    fixed = (repo / "test_slugify.py").read_bytes()
    assert b"{x!r}" in fixed and fixed.index(b"import os") < fixed.index(b"import sys")
    assert (b"\r\n" in fixed) == (newline == "\r\n")                       # the file keeps its own line ends
    assert reviewer.check_ruff(repo, "HEAD", reviewer.changes(repo, base, "HEAD")) == ([], "pass")


def test_autofix_without_a_repository_config_matches_the_review_s_isolated_run(tmp_path):
    write(tmp_path, "pyproject.toml", '[tool.ruff.lint]\nselect = ["E"]\n')     # a parent project's config must not apply
    repo = make_repo(tmp_path / "parent" / "repo")
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "extra.py", "import os\n\n\ndef f():\n    return 1\n")         # F401 is fixable under ruff's own defaults
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker")
    assert run.autofix_lint(repo, base) == ["extra.py"]
    assert "import os" not in (repo / "extra.py").read_text(encoding="utf-8")
    assert reviewer.check_ruff(repo, "HEAD", reviewer.changes(repo, base, "HEAD")) == ([], "pass")


def test_the_review_s_ruff_reads_crlf_files_as_they_are(repo):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "pyproject.toml", '[tool.ruff.lint]\nselect = ["F", "I"]\n')
    write(repo, "crlf.py", "import os\nimport sys\n\nprint(os, sys, undefined_name)\n", newline="\r\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "crlf")
    found, state = reviewer.check_ruff(repo, "HEAD", reviewer.changes(repo, base, "HEAD"))
    assert state == "fail" and [(f.message.split()[0], f.line) for f in found] == [("F821", 4)]   # no phantom I001, the real line


# ---- 3: the plan's test file -------------------------------------------------------------------------------------------
def test_the_worker_prompt_lists_the_proof_s_test_file_and_asks_for_pytest_tests(repo, tmp_path):
    prompts = []

    async def drive(prompt, wt):
        prompts.append(prompt)
        return "nothing to do"

    spec = {"plan_id": "p", "goal": "add slugify", "id": "t", "key": "conductor:p:t", "title": "Add slugify", "detail": "",
            "files": ["textutil.py"], "proof": {"command": "python -m pytest tests/test_textutil.py -q"},
            "branch": "feat/t", "worktree": str(tmp_path / "wt"), "base": "main", "after": []}
    asyncio.run(run.AgentLoopWorker(repo, drive=drive, shell="none")(spec, ""))
    assert "Files likely involved: textutil.py, tests/test_textutil.py" in prompts[0]
    assert "Put the tests in exactly tests/test_textutil.py" in prompts[0] and "do not write a test script" in prompts[0]
    assert "do not invent expected values" in prompts[0]           # factory task 2 failed on an invented "Café" expectation


def test_a_proof_naming_a_missing_file_says_so_and_where_the_tests_went(repo):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "test_slugify.py", "print('PASS')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker")
    spec = {"proof": {"command": "python -m pytest tests/test_textutil.py -q"}}
    bad = asyncio.run(run.run_proof(spec, str(repo), base=base))
    assert bad.rule == "proof" and "not allowed" not in bad.message
    assert "tests/test_textutil.py, which does not exist on your branch" in bad.message and "test_slugify.py" in bad.message
    assert proof.missing_targets("python -m pytest tests/test_upper.py::test_upper", repo) == []


def test_a_proof_not_run_after_a_rejection_is_not_reported_as_passed(repo, tmp_path, monkeypatch):
    async def model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "a", "title": "A", "proof": "python -m pytest tests/test_upper.py -q"}]}), "m/x"

    async def drive(prompt, wt):
        write(wt, "textutil.py", "import os\n\n\ndef upper(text):\n    return text.upper()\n")
        return "done"                                                   # code changed, no test: the review rejects

    started = []
    monkeypatch.setattr(proof, "run", lambda *a, **k: started.append(a))
    llm.set_completer(model)
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    try:
        cond = run.Conductor(repo, run.AgentLoopWorker(repo, drive=drive), board=board, workdir=tmp_path / "wt")
        cond.cfg = {**cond.cfg, "check_ruff": False, "public_repo": False, "model_review": False, "max_rejections": 1}
        plan = asyncio.run(cond.plan("a"))
        asyncio.run(cond.run(plan, integrate=False))
        checks = store.list_reviews(plan.id)[0]["report"]["checks"]
        assert checks["proof"].startswith("skipped") and not started
    finally:
        board.close()


def test_the_proof_env_falls_back_to_abp_s_python_for_a_worker_without_one(repo):
    env = run.Conductor(repo, SimpleNamespace(shell="none"))._proof_env()
    with env():
        assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
