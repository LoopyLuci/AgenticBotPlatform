"""Agent and Factory in Local Chat (PLAN-local-chat.md P4): a real native-agent Build turn edits a temp repo through an approval;
autonomy 0-5 maps onto permission modes and rules (destructive commands always ask, pushing never happens from a turn); cloud
assist Off never leaves the machine (and works with the network down); Factory mode plans, waits for the owner's confirmed Run,
refuses without the sandbox, runs (scripted workers here) with the workers as linked sessions and the result in the push queue;
the context endpoint feeds the meter."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.agent_runtime import approval, permissions
from bot.agent_runtime.transports.base import NormalizedResponse, ToolCall
from bot.backends.base import BackendResult
from bot.localchat import autonomy, chat, factory, links, store

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"
ID = ["-c", "user.name=T", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]
H = {"X-Dashboard-Token": "test-token"}


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
    (r / "tests").mkdir()
    (r / "tests" / "test_app.py").write_text("from app import X\n\n\ndef test_x():\n    assert X\n", encoding="utf-8")
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


class Transport:
    """A scripted model for the real native agent: the given responses, in order."""
    supports_vision = False
    supports_documents = False

    def __init__(self, responses):
        self.responses = list(responses)

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": f"tool result: {out}"} for _, out in results]

    async def send(self, **kw):
        return self.responses.pop(0)


def _native(monkeypatch, responses, seen):
    """router.ask -> ABP's real NativeAgentBackend on a scripted transport, with Local Chat's own context."""
    from bot.backends.native_backend import NativeAgentBackend
    from bot.localchat import catalog
    from bot.router import router

    async def ask(prompt, **kw):
        seen.append(kw)
        ctx = {**(kw.get("context") or {}), "instance_id": kw.get("instance_id"), "chat_id": kw.get("chat_id")}
        return await NativeAgentBackend(Transport(responses), model="scripted").ask(prompt, context=ctx, timeout_s=60)

    monkeypatch.setattr(router, "ask", ask)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": []})


def _approving(frames, outcome="once"):
    async def broadcast(frame):
        frames.append(frame)
        if frame.get("type") == "local_chat_approval":           # the owner presses Allow in the inline card
            approval.resolve(frame["approval_id"], outcome, actor="owner (test)")
    return broadcast


def _write_turn(path="hello.txt", content="hi from the agent\n"):
    return [NormalizedResponse(text="", tool_calls=[ToolCall("c1", "write_file", {"path": path, "content": content})],
                               assistant_message={"role": "assistant", "content": "writing"}),
            NormalizedResponse(text="Wrote it.", assistant_message={"role": "assistant", "content": "Wrote it."})]


def test_a_build_turn_edits_a_temp_repo_through_an_approval(repo, client, monkeypatch):
    seen, frames = [], []
    _native(monkeypatch, _write_turn(), seen)
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "model": CODER}, headers=H).json()
    wt = Path(s["worktree"]["path"])

    async def go():
        await chat.send(s["id"], "create hello.txt", _approving(frames))
        await asyncio.wait_for(chat._running[s["id"]], 30)
    asyncio.run(go())
    asks = [f for f in frames if f.get("type") == "local_chat_approval"]
    assert asks and asks[0]["tool"] == "write_file" and asks[0]["preview"]["summary"].startswith("Write hello.txt")
    assert (wt / "hello.txt").read_text(encoding="utf-8") == "hi from the agent\n"     # in the session's worktree
    assert not (repo / "hello.txt").exists()                                           # never in the base checkout
    statuses = [e["data"]["status"] for e in store.events(session_id=s["id"]) if e["kind"] == "status"]
    assert statuses[:3] == ["working", "needs_you", "working"] and statuses[-1] == "done"
    assert store.messages(s["id"])[-1]["text"] == "Wrote it."
    ctx = seen[0]["context"]
    assert ctx["permission_mode"] == "default" and any(r["match"] == "git push*" and r["decision"] == "deny" for r in ctx["permission_rules"])


def test_trusted_autonomy_edits_without_asking(repo, client, monkeypatch):
    seen, frames = [], []
    _native(monkeypatch, _write_turn("t.txt", "x\n"), seen)
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "model": CODER, "autonomy": 3}, headers=H).json()

    async def go():
        await chat.send(s["id"], "write t.txt", _approving(frames))
        await asyncio.wait_for(chat._running[s["id"]], 30)
    asyncio.run(go())
    assert not [f for f in frames if f.get("type") == "local_chat_approval"]
    assert (Path(s["worktree"]["path"]) / "t.txt").exists() and seen[0]["context"]["permission_mode"] == "accept_edits"


@pytest.mark.parametrize("level,tool,inp,expected", [
    (0, "read_file", {"path": "a.py"}, "ask"),
    (1, "read_file", {"path": "a.py"}, "default"),
    (1, "write_file", {"path": "a.py", "content": ""}, "ask"),
    (1, "run_shell", {"command": "python -m pytest -q"}, "ask"),
    (3, "write_file", {"path": "a.py", "content": ""}, "allow"),
    (3, "run_shell", {"command": "python -m pytest -q"}, "default"),
    (4, "run_shell", {"command": "python -m pytest -q"}, "allow"),
    (4, "run_shell", {"command": "curl example.org"}, "default"),
    (5, "run_shell", {"command": "curl example.org"}, "allow"),
    (5, "run_shell", {"command": "python -m pytest -q; curl x"}, "default"),     # a compound command never matches an allow
    (5, "run_shell", {"command": "rm -rf build"}, "ask"),                         # destructive: always asks
    (5, "run_shell", {"command": "git reset --hard HEAD~1"}, "ask"),
    (5, "run_shell", {"command": "git push origin main"}, "deny"),                # pushing: never from a turn
    (2, "run_shell", {"command": "git push"}, "deny"),
])
def test_autonomy_levels_map_onto_permissions(level, tool, inp, expected):
    pol = autonomy.policy(level)
    verdict = permissions.decide(tool, inp, rules=permissions.parse_rules(pol["rules"]), mode=pol["permission_mode"])
    assert verdict.decision == expected, (level, tool, inp, verdict)


def test_run_level_rules_reach_the_permission_check_and_a_locked_host_keeps_only_deny_and_ask(monkeypatch):
    token = permissions.rules_var.set(tuple(autonomy.policy(5)["rules"]))
    try:
        mode, rules, _ = permissions.effective(None)
        assert any(r.decision == "allow" and r.match == "*" for r in rules)
        monkeypatch.setattr(permissions, "_config", lambda: {"locked": True})
        mode, rules, _ = permissions.effective(None)
        assert rules and not any(r.decision == "allow" for r in rules) and any(r.match == "git push*" for r in rules)
    finally:
        permissions.rules_var.reset(token)
    assert permissions.effective(None)[1] == []


# ---- cloud assist ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def fake_router(temp_db, monkeypatch):
    from bot.localchat import catalog
    from bot.router import router

    calls = []

    async def ask(prompt, **kw):
        calls.append(kw)
        return BackendResult(text="ok", tokens=7)

    monkeypatch.setattr(router, "ask", ask)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": []})
    return calls


def _turn(sid, text, cloud=False):
    async def go():
        r = await chat.send(sid, text, _approving([]), cloud=cloud)
        await asyncio.wait_for(chat._running[sid], 10)
        return r
    return asyncio.run(go())


def test_cloud_assist_off_stays_local_even_with_the_network_down(fake_router, monkeypatch):
    real_connect = socket.socket.connect

    def no_network(self, addr):
        host = addr[0] if isinstance(addr, tuple) else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("network is down (test)")
        return real_connect(self, addr)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    s = store.create(model=CODER, mode="chat", cloud_assist="off")
    with pytest.raises(ValueError, match="cloud assist is off"):
        asyncio.run(chat.send(s["id"], "ask the cloud", _approving([]), cloud=True))
    r = _turn(s["id"], "hello")
    assert r["cloud"] is False and fake_router[-1]["backend_override"] == "native_agent"
    assert fake_router[-1]["model_override"] == "modelmistress/" + CODER
    assert store.messages(s["id"])[-1]["text"] == "ok"


def test_cloud_assist_ask_sends_one_message_to_the_cloud_and_auto_follows_the_governor(fake_router, monkeypatch):
    s = store.create(model=CODER, mode="chat", cloud_assist="ask")
    r = _turn(s["id"], "a hard question", cloud=True)
    assert r["cloud"] is True and "backend_override" not in fake_router[-1] and "model_override" not in fake_router[-1]
    last = store.messages(s["id"])[-1]
    assert last["model"] == chat.CLOUD and last["meta"]["cloud"] is True and last["tokens"] == 7
    assert _turn(s["id"], "an easy one")["cloud"] is False                       # Ask: only when asked
    from bot.capacity import governor

    auto = store.create(model=CODER, mode="chat", cloud_assist="auto")
    monkeypatch.setattr(governor, "effective_state", lambda now=None: governor.CLOUD_LED)
    assert _turn(auto["id"], "x")["cloud"] is True
    monkeypatch.setattr(governor, "effective_state", lambda now=None: governor.LOCAL_ONLY)
    assert _turn(auto["id"], "y")["cloud"] is False


def test_the_context_endpoint_feeds_the_meter(fake_router, client, monkeypatch):
    from bot.localchat import catalog

    s = store.create(model=CODER, mode="review", autonomy=1, cloud_assist="ask")
    store.add_message(s["id"], "user", "x" * 4000)
    store.add_message(s["id"], "assistant", "y" * 400, model=CODER, tokens=120)
    store.add_message(s["id"], "assistant", "z", model=chat.CLOUD, tokens=30)
    monkeypatch.setattr(catalog, "get", lambda **k: {"models": [{"id": CODER, "plan": {"n_ctx_per_slot": 32768}}]})
    d = client.get(f"/api/local-chat/sessions/{s['id']}/context", headers=H).json()
    assert d["meter"]["used"] == (4000 + 400 + 1) // 4 and d["meter"]["limit"] == 32768 and "estimated" in d["meter"]["source"]
    assert d["tokens"] == {"local": 120, "cloud": 30}
    assert d["autonomy"]["label"] == "Cautious" and isinstance(d["tools"], list) and "write_file" not in d["tools"]


# ---- Factory mode ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def planner_model():
    from bot.conductor import llm

    async def model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "flag", "title": "Add a flag", "files": ["flag.py"], "proof": "python -m pytest tests -q"}]}), "m/coder"
    llm.set_completer(model)
    yield
    llm.set_completer(None)


@pytest.fixture
def board(tmp_path):
    from bot import coordination
    from bot.coordination.local import LocalBoard

    b = LocalBoard(tmp_path / "board.db")
    coordination.set_board(b)
    yield b
    coordination.set_board(None)
    b.close()


def scripted_worker(repo, tmp_path):
    async def worker(spec, feedback):
        wt = tmp_path / "fw" / spec["id"]
        if not wt.exists():
            git(repo, "worktree", "add", "-q", "-b", spec["branch"], str(wt), spec["base"])
        (wt / "flag.py").write_text("FLAG = True\n", encoding="utf-8", newline="\n")
        (wt / "tests" / "test_flag.py").write_text("from flag import FLAG\n\n\ndef test_flag():\n    assert FLAG\n", encoding="utf-8", newline="\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", spec["title"])
        return {"branch": spec["branch"], "worktree": str(wt), "created": ["flag.py", "tests/test_flag.py"], "summary": "added FLAG"}
    return worker


def test_factory_mode_plans_waits_for_the_owners_run_and_shows_its_workers(repo, fake_router, planner_model, board, tmp_path, monkeypatch, client):
    s = store.create(model=CODER, mode="factory", workspace=str(repo))
    frames = []

    async def plan_it():
        await chat.send(s["id"], "add a flag", _approving(frames))
        await asyncio.wait_for(chat._running[s["id"]], 30)
    asyncio.run(plan_it())
    assert not fake_router                                                       # planning ran no agent turn
    msg = store.messages(s["id"])[-1]
    plan = msg["meta"]["factory_plan"]
    assert plan["tasks"][0]["title"] == "Add a flag" and "Nothing runs until you press Run" in msg["text"]
    assert msg["meta"]["first_run"] is True and store.get(s["id"])["status"] == "done"

    # the first run in a repository asks; without the sandbox nothing runs at all
    r = client.post(f"/api/local-chat/sessions/{s['id']}/factory/run", json={"plan_id": plan["id"]}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["needs_confirmation"] is True
    from bot.conductor import confine

    monkeypatch.setattr(confine, "supported", lambda: False)
    r = client.post(f"/api/local-chat/sessions/{s['id']}/factory/run", json={"plan_id": plan["id"], "confirm": True}, headers=H)
    assert r.status_code == 400 and "sandbox" in r.json()["detail"]
    assert links.children(s["id"]) == []

    # the owner's confirmed Run with (here: scripted) workers
    monkeypatch.setattr(factory, "make_worker", lambda repo_, model: scripted_worker(repo, tmp_path))
    real = factory._conductor

    def conductor(repo_, base, worker, model, on_event=None):
        c = real(repo_, base, worker, model, on_event)
        c.cfg = {**c.cfg, "model_review": False, "check_ruff": False, "check_hotreload": False, "autofix_lint": False, "public_repo": False}
        c.suite = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
        c.run_proofs = False
        return c
    monkeypatch.setattr(factory, "_conductor", conductor)

    async def run_it():
        out = await factory.start(s["id"], plan["id"], _approving(frames), confirm=True)
        await asyncio.wait_for(factory._runs[s["id"]], 120)
        return out
    out = asyncio.run(run_it())
    assert out["tasks"] == 1 and factory.approved(str(repo))
    workers = links.children(s["id"])
    assert [(w["title"], w["link"], w["status"]) for w in workers] == [("Add a flag", "worker", "done")]
    b = factory.board(s["id"])
    assert b["workers"][0]["id"] == workers[0]["id"] and b["queue"] and b["queue"][0]["status"] == "awaiting_approval"
    final = store.messages(s["id"])[-1]
    assert "1 of 1 task(s) accepted" in final["text"] and final["meta"]["queue_id"] == b["queue"][0]["id"]
    assert "nothing pushes by itself" in final["text"]
    assert not git(repo, "remote")                                                 # no remote: nothing could have been pushed
    assert store.get(s["id"])["status"] == "done"


def test_a_run_of_a_plan_an_earlier_run_escalated_asks_first_and_retries_it(repo, fake_router, planner_model, board, tmp_path,
                                                                         monkeypatch):
    # 2026-10-20: the same goal planned again is the same plan, and the old escalation stopped the new run in seconds
    from bot.conductor import store as cstore

    s = store.create(model=CODER, mode="factory", workspace=str(repo))

    async def plan_it():
        await chat.send(s["id"], "add a flag", _approving([]))
        await asyncio.wait_for(chat._running[s["id"]], 30)
    asyncio.run(plan_it())
    plan = store.messages(s["id"])[-1]["meta"]["factory_plan"]
    key = plan["tasks"][0]["key"]
    cstore.escalate(plan["id"], key, "2 review rejections (an earlier run)")
    factory._approve(str(repo), "test")                                          # not a first run: only the escalation asks

    with pytest.raises(factory.NeedsConfirmation, match="earlier run of this plan .*decision for " + key.rsplit(":", 1)[-1]):
        asyncio.run(factory.start(s["id"], plan["id"], _approving([])))
    assert cstore.open_escalation(plan["id"], key) is not None

    monkeypatch.setattr(factory, "make_worker", lambda repo_, model: scripted_worker(repo, tmp_path))
    real = factory._conductor

    def conductor(repo_, base, worker, model, on_event=None):
        c = real(repo_, base, worker, model, on_event)
        c.cfg = {**c.cfg, "model_review": False, "check_ruff": False, "check_hotreload": False, "autofix_lint": False, "public_repo": False}
        c.suite = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
        c.run_proofs = False
        return c
    monkeypatch.setattr(factory, "_conductor", conductor)

    async def run_it():
        await factory.start(s["id"], plan["id"], _approving([]), confirm=True)
        await asyncio.wait_for(factory._runs[s["id"]], 120)
    asyncio.run(run_it())
    assert cstore.open_escalation(plan["id"], key) is None                      # resolved: the task had its fresh attempt
    assert "1 of 1 task(s) accepted" in store.messages(s["id"])[-1]["text"]


def test_a_worker_commit_leaves_python_caches_out(tmp_path, monkeypatch):
    # 2026-10-20: in a repository without a .gitignore the worker's commits carried __pycache__/*.pyc
    from bot.conductor import run as crun

    repo = tmp_path / "r"
    (repo / "__pycache__").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    (repo / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")
    (repo / "stray.pyc").write_bytes(b"\x00")
    for k, v in (("GIT_AUTHOR_NAME", "T"), ("GIT_AUTHOR_EMAIL", "t@example.org"), ("GIT_COMMITTER_NAME", "T"),
                 ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(k, v)
    crun.AgentLoopWorker._commit(repo, {"title": "t", "key": "k"})
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["mod.py"]
