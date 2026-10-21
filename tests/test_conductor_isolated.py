"""bot/conductor/isolated.py: inside ABP itself, a Factory worker's agent turn, proof and sandbox probes run in child processes, so
their process-global environment (another database, approvals answered by policy, taint wiped, config and PATH changed) never
becomes the live server's (2026-10-20: Local Chat sessions answered 404 while a worker ran)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import db
from bot.agent_runtime import approval
from bot.conductor import isolated, proof, run


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def _untouched():
    return db.DB_PATH, os.environ.get("PATH"), approval.request_approval


def test_a_proof_runs_in_a_child_and_leaves_this_process_alone(repo):
    from abp_agenteval import factory

    before = _untouched()
    env = run.ProofEnv(factory.agent_settings(model="proof", shell="none"), sys.executable, isolate=True)
    spec = {"proof": {"command": "python -m pytest tests/test_ok.py -q"}}
    assert asyncio.run(run.run_proof(spec, str(repo), env=env)) is None                 # passed
    assert _untouched() == before


def test_a_refused_proof_is_reported_as_refused_from_the_child(repo):
    from abp_agenteval import factory

    env = run.ProofEnv(factory.agent_settings(model="proof", shell="none"), sys.executable, isolate=True)
    bad = asyncio.run(run.run_proof({"proof": {"command": "curl http://example.invalid"}}, str(repo), env=env))
    assert bad is not None and "proof not allowed" in bad.message


def test_an_isolated_worker_drives_its_turn_through_the_child(repo, monkeypatch):
    seen = {}

    async def fake_turn(prompt, wt, ref, **kw):
        seen.update(ref=ref, wt=wt, **kw)
        return SimpleNamespace(ok=True, reply="done", error="", tokens=5, iterations=1, tool_calls=[], stopped="")

    monkeypatch.setattr(isolated, "turn", fake_turn)
    monkeypatch.setattr(run.llm, "candidates", lambda model: ["modelmistress/coder"])
    monkeypatch.setattr(run.llm, "transport_for_ref", lambda ref: (object(), "coder"))
    worker = run.AgentLoopWorker(repo, shell="none", isolate=True)
    monkeypatch.setattr(worker, "_check_context", lambda ref, name: None)
    res = asyncio.run(worker._drive("p", repo))
    assert res.reply == "done" and seen["ref"] == "modelmistress/coder" and seen["shell"] == "none"
    assert run.Conductor(repo, worker)._proof_env().isolate is True                    # its proofs go to a child too


def test_the_child_reports_a_failure_instead_of_hanging(repo):
    with pytest.raises(isolated.IsolatedError, match="KeyError"):
        asyncio.run(isolated._child("turn", {}, 30))                                    # a request without its fields


def test_local_chat_s_factory_worker_is_isolated(monkeypatch):
    from bot.conductor import confine
    from bot.localchat import factory as lc_factory

    monkeypatch.setattr(confine, "supported", lambda: True)
    worker = lc_factory.make_worker(str(Path(__file__).resolve().parents[1]), "m")
    assert worker.isolate is True


def test_the_cli_worker_stays_in_process(repo):
    assert run.AgentLoopWorker(repo, shell="none").isolate is False
    assert proof.ENV_LOCK is run.AgentLoopWorker._agent_lock
