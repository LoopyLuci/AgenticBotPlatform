"""Selective tests, the scope warning and the CacheIt-backed proof/suite cache (backend injected; no hub needed)."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from bot.codeintel import tools
from bot.conductor import cache as cachelib
from bot.conductor import integrator, proof, reviewer

PASS = [sys.executable, "-c", "pass"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                           *args], check=True, capture_output=True, text=True).stdout


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class Mem:
    """An in-memory stand-in for the CacheIt hub's cache.get / cache.put."""

    def __init__(self):
        self.d, self.calls = {}, []

    def __call__(self, op, args):
        self.calls.append(op)
        if op == "cache.put":
            self.d[args["key"]] = args["value"]
            return {"ok": True}
        if args["key"] in self.d:
            return {"found": True, "value": self.d[args["key"]]}
        return {"found": False}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "pytest.ini", "[pytest]\npythonpath = .\n")
    write(root, "lib.py", "def helper(x):\n    return x + 1\n\n\ndef other():\n    return 0\n")
    write(root, "user.py", "from lib import helper\n\n\ndef go():\n    return helper(1)\n")
    write(root, "tests/test_lib.py", "from lib import helper\n\n\ndef test_helper():\n    assert helper(1) == 2\n")
    write(root, "tests/test_unrelated.py", "def test_x():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def change_helper(root):
    git(root, "checkout", "-q", "-b", "feat/h")
    write(root, "lib.py", "def helper(x):\n    return 1 + x\n\n\ndef other():\n    return 0\n")
    git(root, "commit", "-qam", "h")
    git(root, "checkout", "-q", "main")
    return "feat/h"


def test_a_changed_symbol_maps_to_its_related_tests(repo):
    b = change_helper(repo)
    integ = integrator.Integrator(repo, base="main", suite=PASS, cache=cachelib.Cache(enabled=False), selective_first=True)
    git(repo, "checkout", "-q", b)
    tests, why = integ.related_test_files(repo)
    assert tests == ["tests/test_lib.py"], why


def test_the_integrator_runs_the_selective_tests_then_the_full_suite(repo, tmp_path):
    b = change_helper(repo)
    integ = integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=PASS, selective_first=True,
                                  cache=cachelib.Cache(enabled=False),
                                  rerun=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"])
    rec = integ.integrate([b], persist=False)
    assert rec.status == "green"
    assert rec.selective["tests"] == ["tests/test_lib.py"] and rec.selective["rc"] == 0
    assert rec.shards and rec.shards[0]["rc"] == 0


def test_selective_can_be_switched_off(repo, tmp_path):
    b = change_helper(repo)
    rec = integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=PASS, selective_first=False,
                                cache=cachelib.Cache(enabled=False)).integrate([b], persist=False)
    assert rec.status == "green" and rec.selective == {}


def test_the_reviewer_flags_a_caller_outside_the_declared_files(repo):
    b = change_helper(repo)
    git(repo, "checkout", "-q", b)
    rep = reviewer.review(repo, "main", b, run_ruff=False, check_reload=False, public=False, declared_files=["lib.py"], scope_root=repo)
    assert rep.checks["scope"] == "warn"
    assert any(f.rule == "scope" and f.file == "user.py" for f in rep.advisory)
    assert rep.verdict == reviewer.ACCEPT or all(f.rule != "scope" for f in rep.findings)
    strict = reviewer.review(repo, "main", b, run_ruff=False, check_reload=False, public=False, declared_files=["lib.py"],
                             scope_root=repo, scope_fail=True)
    assert any(f.rule == "scope" for f in strict.findings) and strict.verdict == reviewer.REJECT
    ok = reviewer.review(repo, "main", b, run_ruff=False, check_reload=False, public=False, declared_files=["lib.py", "user.py"],
                         scope_root=repo)
    assert ok.checks["scope"] == "pass"


def test_a_proof_hits_the_cache_on_an_identical_tree_without_starting_a_process_and_misses_after_an_edit(repo, monkeypatch):
    from bot.agent_runtime import shell

    started = []

    async def fake_run(cmd, cwd, root, limit):
        started.append(cmd)
        return "1 passed in 0.01s\n[exit code 0]"

    monkeypatch.setattr(shell, "_run_fresh", fake_run)
    mem = Mem()
    c = cachelib.Cache(backend=mem)
    cmd = "python -m pytest tests/test_unrelated.py -q"
    assert asyncio.run(proof.run(cmd, repo, cache=c))[0] is True
    assert len(started) == 1
    assert asyncio.run(proof.run(cmd, repo, cache=c))[0] is True
    assert len(started) == 1                                    # a hit: nothing started
    write(repo, "lib.py", "def helper(x):\n    return 99\n")
    assert asyncio.run(proof.run(cmd, repo, cache=c))[0] is True
    assert len(started) == 2                                    # the tree changed: a miss


def test_a_failing_proof_is_never_cached(repo, monkeypatch):
    from bot.agent_runtime import shell

    n = []

    async def fake_run(cmd, cwd, root, limit):
        n.append(1)
        return "1 failed in 0.01s\n[exit code 1]"

    monkeypatch.setattr(shell, "_run_fresh", fake_run)
    c = cachelib.Cache(backend=Mem())
    for _ in range(2):
        assert asyncio.run(proof.run("python -m pytest tests/test_unrelated.py -q", repo, cache=c))[0] is False
    assert len(n) == 2


def test_cacheit_being_down_just_runs_normally(repo, monkeypatch):
    from bot.agent_runtime import shell

    n = []

    async def fake_run(cmd, cwd, root, limit):
        n.append(1)
        return "1 passed in 0.01s\n[exit code 0]"

    def boom(op, args):
        raise ConnectionError("hub down")

    monkeypatch.setattr(shell, "_run_fresh", fake_run)
    c = cachelib.Cache(backend=boom)
    for _ in range(2):
        assert asyncio.run(proof.run("python -m pytest tests/test_unrelated.py -q", repo, cache=c))[0] is True
    assert len(n) == 2
    c2 = cachelib.Cache()                                        # no injected backend and no hub for the module: still no crash
    monkeypatch.setattr(cachelib, "_module_backend", lambda: (_ for _ in ()).throw(OSError("no hub")))
    assert c2.lookup("t", "cmd") is None and c2.store("t", "cmd", (), {"rc": 0}) is False


def test_suite_results_are_cached_by_tree(repo, tmp_path):
    mem = Mem()
    b = change_helper(repo)

    def go(i):
        return integrator.Integrator(repo, base="main", workdir=tmp_path / f"wt{i}", suite=PASS, selective_first=False,
                                     cache=cachelib.Cache(backend=mem)).integrate([b], persist=False)

    first = go(1)
    assert first.cache["stores"] == 1 and first.cache["hits"] == 0
    # a second integration of the same branch produces a different merge commit but we key on tree content: same tree -> hit
    second = go(2)
    assert second.status == "green" and second.cache["hits"] == 1 and second.shards[0]["cached"] is True


def test_impact_tool_still_names_the_test(repo):
    assert "tests/test_lib.py" in tools.impact(repo, "lib.py").data["tests"]
