"""Local Chat sessions and worktrees (PLAN-local-chat.md 4b, S1), against a real temporary git repository: a session's worktree is
created, edited by a Build turn, diffed, committed, synced after the base moves (a conflict is handed to the agent), rewound,
and discarded only with confirmation; status / unread / events, groups, steering and queueing, archive, prune, the push queue,
the live-checkout guard and the migration of a P1 database."""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.backends.base import BackendResult
from bot.localchat import chat, store, worktrees

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"
ID = ["-c", "user.name=T", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    r = subprocess.run(["git", *ID, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path / "abp-root")       # the shadow checkpoint store
    from bot.modules import registry

    monkeypatch.setattr(registry, "abp_root", lambda: tmp_path / "abp-root")      # "the live checkout" is elsewhere


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    (r / "src").mkdir()
    (r / "src" / "util.py").write_text("X = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


class Agent:
    """A stand-in for router.ask: runs `plan(cwd, prompt)` (the "tools") and records what it was asked."""

    def __init__(self):
        self.calls, self.plan, self.gate = [], None, None

    async def __call__(self, prompt, **kw):
        ctx = kw.get("context") or {}
        self.calls.append({"prompt": prompt, **kw})
        if self.gate is not None:
            await self.gate.wait()
        if self.plan:
            self.plan(Path(ctx["cwd"]), prompt, ctx)
        return BackendResult(text="done", tokens=10)


@pytest.fixture
def agent(temp_db, monkeypatch):
    from bot.localchat import catalog
    from bot.router import router

    a = Agent()
    monkeypatch.setattr(router, "ask", a)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": []})
    return a


def _bc(frames):
    async def broadcast(frame):
        frames.append(frame)
    return broadcast


async def _turn(sid, text, frames):
    await chat.send(sid, text, _bc(frames))
    await asyncio.wait_for(chat._running[sid], 10)


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


H = {"X-Dashboard-Token": "test-token"}


# ---- the exit test: worktree -> Build turn -> diff -> commit -> sync with a conflict -> rewind -> discard -----------------------
def test_a_sessions_worktree_end_to_end(repo, agent, client, monkeypatch):
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo / "src"), "model": CODER}, headers=H).json()
    wt = s["worktree"]
    assert wt and wt["base"] == "main" and wt["branch"].startswith("chat/") and wt["rel"] == "src"
    path = Path(wt["path"])
    assert path.is_dir() and path.resolve() != repo.resolve() and repo not in path.parents   # outside the repository
    assert git(repo, "status", "--porcelain") == ""                                        # the base checkout is untouched

    # a Build turn edits app.py inside the session's worktree (its cwd is the worktree's src/)
    def edit(cwd, prompt, ctx):
        assert Path(cwd) == path / "src"
        (cwd.parent / "app.py").write_text("def greet():\n    return 'hi there'\n", encoding="utf-8")
    agent.plan = edit
    frames = []
    asyncio.run(_turn(s["id"], "make the greeting friendlier", frames))
    assert (repo / "app.py").read_text(encoding="utf-8").endswith("'hello'\n")            # base untouched
    st = client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json()["worktree"]
    assert st["dirty"] == ["app.py"] and st["ahead"] == 0 and st["unmerged_work"]
    d = client.get(f"/api/local-chat/sessions/{s['id']}/worktree/diff", headers=H).json()
    assert "-    return 'hello'" in d["diff"] and "+    return 'hi there'" in d["diff"]

    # syncing with uncommitted work is refused (nothing in the tree is ever clobbered); commit with the drafted message
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/sync", json={}, headers=H).status_code == 400
    draft = client.get(f"/api/local-chat/sessions/{s['id']}/worktree/commit-message", headers=H).json()
    assert "app.py" in draft["message"]
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/commit", json={"message": draft["message"]}, headers=H).status_code == 200
    assert client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json()["worktree"]["ahead"] == 1

    # the base moves with a conflicting change; sync lists the conflict and hands it to the agent
    (repo / "app.py").write_text("def greet():\n    return 'hello, world'\n", encoding="utf-8")
    git(repo, "commit", "-qam", "base moves")
    assert client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json()["worktree"]["behind"] == 1
    handed, real_send = [], chat.send

    async def fake_send(sid, text, broadcast):           # the hand-off itself; turns are covered by the tests below
        handed.append((sid, text))
        return {"turn_id": "t-conflict"}
    monkeypatch.setattr(chat, "send", fake_send)
    res = client.post(f"/api/local-chat/sessions/{s['id']}/worktree/sync", json={"hand_to_agent": True}, headers=H).json()
    assert res["conflicts"] == ["app.py"] and res["merged"] is False and res["turn"]["turn_id"] == "t-conflict"
    assert handed and handed[0][0] == s["id"] and "app.py" in handed[0][1] and "conflict" in handed[0][1]
    monkeypatch.setattr(chat, "send", real_send)
    st = client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json()["worktree"]
    assert st["merging"] and st["conflicts"] == ["app.py"]
    msgs = [m["text"] for m in store.messages(s["id"])]
    assert any("stopped on conflicts in: app.py" in t for t in msgs)

    # the agent resolves; commit concludes the merge
    (path / "app.py").write_text("def greet():\n    return 'hi there, world'\n", encoding="utf-8")
    git(path, "add", "app.py")
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/commit", json={"message": "merge main"}, headers=H).status_code == 200
    st = client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json()["worktree"]
    assert st["behind"] == 0 and not st["merging"]

    # rewind: a turn that breaks things is undone, its messages stay (marked)
    def vandal(cwd, prompt, ctx):
        (cwd.parent / "app.py").write_text("broken\n", encoding="utf-8")
        (cwd / "junk.txt").write_text("junk", encoding="utf-8")
    agent.plan = vandal
    asyncio.run(_turn(s["id"], "break it", frames))
    tid = store.turns(s["id"])[-1]["id"]
    assert (path / "app.py").read_text(encoding="utf-8") == "broken\n"
    r = client.post(f"/api/local-chat/sessions/{s['id']}/rewind", json={"turn_id": tid}, headers=H).json()
    assert r["messages_reverted"] == 2 and r["undo_point"]
    assert (path / "app.py").read_text(encoding="utf-8") == "def greet():\n    return 'hi there, world'\n"
    assert not (path / "src" / "junk.txt").exists()
    reverted = [m for m in store.messages(s["id"]) if m["reverted"]]
    assert [m["text"] for m in reverted] == ["break it", "done"]

    # discarding with unmerged work asks first and says what would be lost; with confirmation it goes
    r = client.post(f"/api/local-chat/sessions/{s['id']}/worktree/discard", json={}, headers=H)
    assert r.status_code == 409 and len(r.json()["detail"]["would_lose"]["commits"]) >= 1
    assert path.is_dir()
    assert client.delete(f"/api/local-chat/sessions/{s['id']}", headers=H).status_code == 409       # delete asks too
    r = client.post(f"/api/local-chat/sessions/{s['id']}/worktree/discard", json={"confirm": True}, headers=H).json()
    assert r["discarded"] and not path.exists()
    assert wt["branch"] not in git(repo, "branch", "--list")
    assert client.get(f"/api/local-chat/sessions/{s['id']}/worktree", headers=H).json() == {"worktree": None}


# ---- status, unread, events ---------------------------------------------------------------------------------------------------
def test_status_moves_with_the_turn_and_unread_until_read(agent, client):
    s = store.create(model=CODER, mode="chat")
    frames = []
    asyncio.run(_turn(s["id"], "hi", frames))
    got = store.get(s["id"])
    assert got["status"] == "done" and got["unread"] is True
    seen = [f["session"]["status"] for f in frames if f.get("type") == "local_chat_session"]
    assert seen == ["working", "done"]
    kinds = [(e["kind"], e["data"]["status"]) for e in store.events(session_id=s["id"])]
    assert kinds == [("status", "working"), ("status", "done")]
    r = client.post(f"/api/local-chat/sessions/{s['id']}/read", headers=H).json()
    assert r["unread"] is False and r["last_read_id"] == store.messages(s["id"])[-1]["id"]
    assert client.get("/api/local-chat/events?after=0", headers=H).json()["events"]


def test_an_approval_puts_the_session_on_needs_you_and_back(agent):
    s = store.create(model=CODER, mode="build")
    frames = []

    def asks(cwd, prompt, ctx):
        pass

    async def go():
        agent.gate = asyncio.Event()
        await chat.send(s["id"], "edit", _bc(frames))
        for _ in range(100):
            if agent.calls:
                break
            await asyncio.sleep(0.01)
        ctx = agent.calls[-1]["context"]
        await ctx["approval_notify"](7, "write_file", {"path": "a.txt", "content": "x"})
        assert store.get(s["id"])["status"] == "needs_you"

        class Ev:
            kind, text = "text", "ok"
        await ctx["stream_notify"](Ev())
        assert store.get(s["id"])["status"] == "working"
        agent.gate.set()
        await asyncio.wait_for(chat._running[s["id"]], 10)
    asyncio.run(go())
    assert any(f.get("type") == "local_chat_approval" and f["approval_id"] == 7 for f in frames)


def test_a_restart_never_leaves_a_session_working():
    s = store.create()
    store.set_status(s["id"], "working")
    store.start_turn(s["id"])
    store.recover()
    assert store.get(s["id"])["status"] == "error"


# ---- groups ----------------------------------------------------------------------------------------------------------------
def test_groups_and_moving_sessions_between_them(client):
    g = client.post("/api/local-chat/groups", json={"name": "  Factory   work "}, headers=H).json()
    assert g["name"] == "Factory work"
    s = client.post("/api/local-chat/sessions", json={"mode": "chat"}, headers=H).json()
    assert client.patch(f"/api/local-chat/sessions/{s['id']}", json={"group_id": g["id"]}, headers=H).json()["group_id"] == g["id"]
    assert client.patch(f"/api/local-chat/sessions/{s['id']}", json={"group_id": "nope"}, headers=H).status_code == 400
    assert client.patch(f"/api/local-chat/groups/{g['id']}", json={"name": "LF"}, headers=H).json()["name"] == "LF"
    assert client.post("/api/local-chat/groups", json={"name": " "}, headers=H).status_code == 400
    assert client.delete(f"/api/local-chat/groups/{g['id']}", headers=H).json() == {"deleted": True}
    assert store.get(s["id"])["group_id"] == ""                                                      # the session stays
    assert client.get("/api/local-chat/groups", headers=H).json() == {"groups": []}


# ---- steer and queue --------------------------------------------------------------------------------------------------------
def test_steer_reaches_the_running_turn_and_queue_waits_for_the_next(agent):
    s = store.create(model=CODER, mode="chat")
    frames = []
    seen_steer = []

    def look(cwd, prompt, ctx):
        q = ctx["steer_queue"]
        while not q.empty():
            seen_steer.append(q.get_nowait())
    agent.plan = None

    async def go():
        agent.gate = asyncio.Event()
        await chat.send(s["id"], "first", _bc(frames))
        await asyncio.sleep(0.05)
        assert chat.steer(s["id"], "also do X", "steer") == {"steered": True, "queued": False}
        assert chat.steer(s["id"], "then do Y", "queue")["queued"] is True
        q = agent.calls[-1]["context"]["steer_queue"]
        while not q.empty():
            seen_steer.append(q.get_nowait())
        agent.gate.set()
        await asyncio.wait_for(chat._running[s["id"]], 10)
        for _ in range(100):                          # the queued message starts by itself
            if len(agent.calls) == 2 and not chat.is_running(s["id"]):
                break
            await asyncio.sleep(0.02)
        if chat.is_running(s["id"]):
            await asyncio.wait_for(chat._running[s["id"]], 10)
    asyncio.run(go())
    assert seen_steer == ["also do X"]
    assert [c["prompt"] for c in agent.calls] == ["first", "then do Y"]
    assert store.queued(s["id"]) == []
    steered = [m for m in store.messages(s["id"]) if m["meta"].get("steered")]
    assert [m["text"] for m in steered] == ["also do X"]
    with pytest.raises(ValueError):
        chat.steer(s["id"], "x", "shout")


# ---- archive, prune, the push queue ------------------------------------------------------------------------------------------
def test_archiving_removes_an_unchanged_worktree_and_keeps_a_changed_one(repo, client):
    a = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    b = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    (Path(b["worktree"]["path"]) / "new.txt").write_text("work", encoding="utf-8")
    ra = client.patch(f"/api/local-chat/sessions/{a['id']}", json={"archived": True}, headers=H).json()
    rb = client.patch(f"/api/local-chat/sessions/{b['id']}", json={"archived": True}, headers=H).json()
    assert ra["archive"] == {"worktree": "removed"} and not Path(a["worktree"]["path"]).exists()
    assert rb["archive"]["worktree"] == "kept" and Path(b["worktree"]["path"]).is_dir()
    assert ra["archived_at"]
    c = client.post("/api/local-chat/sessions", json={"mode": "chat", "workspace": str(repo)}, headers=H).json()
    assert c["worktree"] is None                                                       # Chat mode: no worktree by default
    d = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "worktree": False}, headers=H).json()
    assert d["worktree"] is None


def test_prune_removes_orphans_and_merged_clean_worktrees_but_never_work(repo, client):
    a = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    b = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    c = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    (Path(b["worktree"]["path"]) / "wip.txt").write_text("wip", encoding="utf-8")
    store.delete(a["id"])                                     # a's folder is now an orphan; b's is an orphan with work
    store.delete(b["id"])
    r = client.post("/api/local-chat/worktrees/prune", json={"clean_merged": True}, headers=H).json()
    removed = {x["path"]: x["why"] for x in r["removed"]}
    assert removed[a["worktree"]["path"]].startswith("orphan") and not Path(a["worktree"]["path"]).exists()
    assert any(k["path"] == b["worktree"]["path"] and "uncommitted" in k["why"] for k in r["kept"])
    assert removed[c["worktree"]["path"]].startswith("merged")                   # no commits of its own: merged into main


def test_queue_merge_goes_through_the_conductors_push_queue_and_pushes_nothing(repo, client):
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo)}, headers=H).json()
    path = Path(s["worktree"]["path"])
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/queue-merge", headers=H).status_code == 400    # nothing to merge
    (path / "feature.py").write_text("ok = True\n", encoding="utf-8")
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/queue-merge", headers=H).status_code == 400    # uncommitted
    client.post(f"/api/local-chat/sessions/{s['id']}/worktree/commit", json={"message": "feature"}, headers=H)
    entry = client.post(f"/api/local-chat/sessions/{s['id']}/worktree/queue-merge", headers=H).json()["queued"]
    assert entry["status"] == "awaiting_approval" and entry["branch"] == s["worktree"]["branch"] and entry["base"] == "main"
    assert git(repo, "log", "--oneline", "main").count("\n") == 0                         # main still has its one commit
    from bot.conductor import store as cstore

    assert cstore.get_queue_entry(entry["id"])["head_sha"] == git(path, "rev-parse", "HEAD")


# ---- guards -------------------------------------------------------------------------------------------------------------------
def test_build_never_works_in_the_live_checkout_without_a_worktree(repo, agent, monkeypatch):
    from bot.modules import registry

    monkeypatch.setattr(registry, "abp_root", lambda: repo)
    s = store.create(model=CODER, mode="build", workspace=str(repo))
    with pytest.raises(ValueError, match="live ABP checkout"):
        asyncio.run(chat.send(s["id"], "edit things", _bc([])))
    review = store.create(model=CODER, mode="review", workspace=str(repo))
    asyncio.run(_turn(review["id"], "look", []))                                  # reading it is fine
    wt = worktrees.create(s["id"])
    assert Path(wt["path"]).resolve() != repo.resolve()
    asyncio.run(_turn(s["id"], "edit things", []))                                # in its worktree it works


def test_a_worktree_needs_a_git_workspace(tmp_path, client):
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(tmp_path)}, headers=H).json()
    assert s["worktree"] is None
    r = client.post(f"/api/local-chat/sessions/{s['id']}/worktree", json={}, headers=H)
    assert r.status_code == 400 and "git repository" in r.json()["detail"]
    r = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(tmp_path), "worktree": True}, headers=H)
    assert r.status_code == 400


def test_rewind_needs_a_checkpoint_and_a_stopped_turn(agent, client):
    s = store.create(model=CODER, mode="chat")
    asyncio.run(_turn(s["id"], "hi", []))
    tid = store.turns(s["id"])[-1]["id"]
    assert client.post(f"/api/local-chat/sessions/{s['id']}/rewind", json={"turn_id": tid}, headers=H).status_code == 404


# ---- migration --------------------------------------------------------------------------------------------------------------
def test_a_p1_database_is_migrated_in_place():
    path = store.db_path()
    conn = sqlite3.connect(str(path))
    conn.executescript("""
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    model TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'build', autonomy INTEGER NOT NULL DEFAULT 2,
    workspace TEXT NOT NULL DEFAULT '', cloud_assist TEXT NOT NULL DEFAULT 'ask', pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created REAL NOT NULL, turn_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0, tokens_per_s REAL NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}');
INSERT INTO sessions (id, title, created, updated) VALUES ('old1', 'from P1', 1, 1);
INSERT INTO messages (session_id, role, text, created) VALUES ('old1', 'user', 'hello', 1);
""")
    conn.commit()
    conn.close()
    s = store.get("old1")
    assert s["title"] == "from P1" and s["status"] == "idle" and s["unread"] is False and s["worktree"] is None
    assert store.messages("old1")[0]["reverted"] is False
    assert store.update("old1", effort="high")["effort"] == "high"
