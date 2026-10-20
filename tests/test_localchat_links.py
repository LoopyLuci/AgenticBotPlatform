"""Linked Local Chat sessions (PLAN-local-chat.md 4b, S3): a fork works in its own worktree cut from the parent's branch tip and
its turn end reaches the parent; a task starts on a brief; a hand-off carries the remaining plan; the children list; and a
Factory (conductor) run shows its workers as read-only linked sessions."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.backends.base import BackendResult
from bot.localchat import chat, links, store, worktrees

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"
ID = ["-c", "user.name=T", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    r = subprocess.run(["git", *ID, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path / "abp-root")
    from bot.modules import registry

    monkeypatch.setattr(registry, "abp_root", lambda: tmp_path / "abp-root")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("X = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def agent(temp_db, monkeypatch):
    from bot.localchat import catalog
    from bot.router import router

    calls = []

    async def ask(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return BackendResult(text=f"Recap: handled '{prompt[-40:]}'", tokens=5)

    monkeypatch.setattr(router, "ask", ask)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": []})
    return calls


def _bc(frames):
    async def broadcast(frame):
        frames.append(frame)
    return broadcast


async def _turn(sid, text, frames):
    await chat.send(sid, text, _bc(frames))
    await asyncio.wait_for(chat._running[sid], 10)


H = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


def test_a_fork_works_in_its_own_worktree_and_its_turn_end_reaches_the_parent(repo, agent, client):
    parent = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "model": CODER}, headers=H).json()
    frames = []
    asyncio.run(_turn(parent["id"], "add a helper", frames))
    ppath = Path(parent["worktree"]["path"])
    (ppath / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    worktrees.commit(parent["id"], "helper")
    (ppath / "scratch.txt").write_text("not committed", encoding="utf-8")       # uncommitted: reported, not carried

    res = client.post(f"/api/local-chat/sessions/{parent['id']}/fork", json={}, headers=H).json()
    child = res["session"]
    assert child["parent_id"] == parent["id"] and child["link"] == "fork" and child["model"] == CODER
    cwt = child["worktree"]
    assert cwt["base"] == parent["worktree"]["branch"] and cwt["path"] != parent["worktree"]["path"]
    assert (Path(cwt["path"]) / "helper.py").exists() and not (Path(cwt["path"]) / "scratch.txt").exists()
    assert res["not_carried"] == ["scratch.txt"]

    # its first turn starts with the parent's conversation as context; later turns do not repeat it
    asyncio.run(_turn(child["id"], "now test it", frames))
    first = agent[-1]["prompt"]
    assert "forked from" in first and "User: add a helper" in first and first.endswith("now test it")
    asyncio.run(_turn(child["id"], "and again", frames))
    assert agent[-1]["prompt"] == "and again"

    # the child's turn end left a note with its recap in the parent, and told the sockets
    notes = [m for m in store.messages(parent["id"]) if (m["meta"] or {}).get("note")]
    assert notes and notes[0]["meta"]["child"] == child["id"] and "Recap:" in notes[0]["text"]
    assert any(f.get("type") == "local_chat_note" and f["session_id"] == parent["id"] and f["child_id"] == child["id"] for f in frames)
    assert store.get(parent["id"])["unread"] is True
    kids = client.get(f"/api/local-chat/sessions/{parent['id']}/children", headers=H).json()["children"]
    assert [(k["id"], k["link"], k["branch"]) for k in kids] == [(child["id"], "fork", cwt["branch"])]


def test_a_task_starts_on_its_brief_and_a_handoff_carries_the_plan(repo, agent, client):
    parent = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "model": CODER}, headers=H).json()
    frames = []
    asyncio.run(_turn(parent["id"], "refactor the parser", frames))
    t = client.post(f"/api/local-chat/sessions/{parent['id']}/task", json={"brief": "write docs for the parser", "start": False},
                    headers=H).json()
    assert t["session"]["link"] == "task" and t["session"]["title"] == "write docs for the parser" and t["worktree"]
    asyncio.run(_turn(t["session"]["id"], t["brief"], frames))
    assert "A task started from the session" in agent[-1]["prompt"] and "User: refactor the parser" not in agent[-1]["prompt"]
    assert client.post(f"/api/local-chat/sessions/{parent['id']}/task", json={"brief": " "}, headers=H).status_code == 400

    h = client.post(f"/api/local-chat/sessions/{parent['id']}/handoff", json={"plan": "1. finish tests\n2. update docs", "start": False},
                    headers=H).json()
    asyncio.run(_turn(h["session"]["id"], "Continue with what is left to do.", frames))
    prompt = agent[-1]["prompt"]
    assert "handed off" in prompt and "1. finish tests" in prompt and "User: refactor the parser" in prompt
    pmsgs = [m["text"] for m in store.messages(parent["id"])]
    assert any(x.startswith("Handed off to") for x in pmsgs) and any(x.startswith("Started the task") for x in pmsgs)
    assert {k["link"] for k in links.children(parent["id"])} == {"task", "handoff"}


def test_worker_sessions_are_read_only(agent):
    parent = store.create(model=CODER, mode="build")
    w = links.register_worker(parent["id"], task_id="p1:t1", title="Add a flag", worktree={"path": "", "branch": "conductor/p1-t1"})
    assert w["link"] == "worker" and w["mode"] == "review" and w["worktree"]["worker"] and w["worktree"]["keep"]
    assert links.register_worker(parent["id"], task_id="p1:t1", title="again")["id"] == w["id"]       # one per task
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(chat.send(w["id"], "hi", _bc([])))
    with pytest.raises(worktrees.WorktreeError, match="conductor"):
        worktrees.discard(w["id"], confirm=True)
    assert links.worker_update(w["id"], status="done", text="Accepted.")["status"] == "done"


def test_a_factory_run_shows_its_workers(repo, agent, tmp_path):
    from bot import coordination
    from bot.conductor import llm, run
    from bot.coordination.local import LocalBoard

    async def plan_model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "flag", "title": "Add a flag", "files": ["flag.py"], "proof": "python -m pytest tests -q"},
                                     {"id": "doc", "title": "Document the flag", "depends_on": ["flag"], "files": ["DOC.md"],
                                      "proof": "python -m pytest tests -q"}]}), "m/coder"

    async def worker(spec, feedback):
        wt = tmp_path / "wt" / spec["id"]
        start = spec["after"][-1] if spec["after"] else spec["base"]
        if not wt.exists():
            git(repo, "worktree", "add", "-q", "-b", spec["branch"], str(wt), start)
        name = "flag.py" if spec["id"] == "flag" else "DOC.md"
        (wt / name).write_text("ok = True\n" if name.endswith(".py") else "The flag.\n", encoding="utf-8", newline="\n")
        if name.endswith(".py"):
            (wt / "tests").mkdir(exist_ok=True)
            (wt / "tests" / "test_flag.py").write_text("from flag import ok\n\n\ndef test_ok():\n    assert ok\n", encoding="utf-8", newline="\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", spec["title"])
        return {"branch": spec["branch"], "worktree": str(wt), "start": start, "created": [name], "summary": f"wrote {name}"}

    parent = store.create(model=CODER, mode="build", workspace=str(repo))
    frames = []
    llm.set_completer(plan_model)
    board = LocalBoard(tmp_path / "board.db")
    coordination.set_board(board)
    try:
        cond = run.Conductor(repo, worker, board=board, workdir=tmp_path / "wt", run_proofs=False,
                             on_event=links.factory_observer(parent["id"], _bc(frames), repo=str(repo), base="main"))
        cond.cfg = {**cond.cfg, "model_review": False, "check_ruff": False, "check_hotreload": False, "autofix_lint": False,
                    "public_repo": False}
        plan = asyncio.run(cond.plan("add a flag and document it"))
        report = asyncio.run(cond.run(plan))
    finally:
        llm.set_completer(None)
        coordination.set_board(None)
        board.close()
    from bot.conductor import store as cstore

    assert [t["status"] for t in report.tasks.values()] == ["success", "success"], [r["report"]["findings"] for t in plan.tasks for r in cstore.list_reviews(plan.id, t.key)]
    workers = links.children(parent["id"])
    assert sorted(w["title"] for w in workers) == ["Add a flag", "Document the flag"]
    assert {w["link"] for w in workers} == {"worker"} and {w["status"] for w in workers} == {"done"}
    flag = next(w for w in workers if w["title"] == "Add a flag")
    texts = [m["text"] for m in store.messages(flag["id"])]
    assert any("Attempt 1 started" in t for t in texts) and any(t.startswith("Review: accept") for t in texts)
    assert any(t.startswith("Accepted on") and "wrote flag.py" in t for t in texts)
    assert flag["branch"].startswith(plan.tasks[0].branch.split("/")[0])
    assert any(f.get("type") == "local_chat_session" and f["session_id"] == flag["id"] for f in frames)
