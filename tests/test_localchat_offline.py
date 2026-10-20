"""Local Chat offline and under stress (PLAN-local-chat.md P6): the whole flow - catalog, a session with its worktree, a real
native-agent Build turn, diff and commit, a Factory plan, the capability index - with the network down; a hub that is down
or degraded is said plainly; one GPU shared between chat and a Factory run; the model picker answers from its cache fast."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.agent_runtime import approval
from bot.agent_runtime.transports.base import NormalizedResponse, ToolCall
from bot.localchat import catalog, chat, factory, store

from test_localchat import CODER, REPORTS, SMALL, _hub

ID = ["-c", "user.name=T", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]
H = {"X-Dashboard-Token": "test-token"}
REAL_BUILD = catalog.build                  # the real catalog builder, fed a stand-in hub below


def git(cwd, *args):
    r = subprocess.run(["git", *ID, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path / "abp-root")
    from bot.modules import registry

    monkeypatch.setattr(registry, "abp_root", lambda: tmp_path / "abp-root")
    catalog.forget()
    yield
    catalog.forget()


@pytest.fixture
def offline(monkeypatch):
    """No network: any connection to something other than this machine fails, and is recorded."""
    tried = []
    real = socket.socket.connect

    def connect(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            tried.append(addr)
            raise OSError("network is down (test)")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", connect)
    real_gai = socket.getaddrinfo

    def gai(host, *a, **k):
        if host not in ("127.0.0.1", "::1", "localhost", None):
            tried.append((host, "dns"))
            raise socket.gaierror("no DNS (test)")
        return real_gai(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    return tried


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
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


class Transport:
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


def test_the_whole_local_chat_flow_works_with_the_network_down(repo, client, offline, monkeypatch):
    from bot.backends.native_backend import NativeAgentBackend
    from bot.conductor import llm
    from bot.router import router

    call, _ = _hub()                                                    # a stand-in hub on this machine
    monkeypatch.setattr(catalog, "build", lambda *a, **k: REAL_BUILD(call, reports=REPORTS, min_context=16384))
    responses = [NormalizedResponse(text="", tool_calls=[ToolCall("c1", "write_file", {"path": "offline.txt", "content": "works\n"})],
                                    assistant_message={"role": "assistant", "content": "w"}),
                 NormalizedResponse(text="Done offline.", assistant_message={"role": "assistant", "content": "Done offline."})]

    async def ask(prompt, **kw):
        assert kw.get("backend_override") == "native_agent" and kw["model_override"].startswith("modelmistress/")   # local only
        ctx = {**kw["context"], "instance_id": kw["instance_id"], "chat_id": kw["chat_id"]}
        return await NativeAgentBackend(Transport(responses), model="local").ask(prompt, context=ctx, timeout_s=60)
    monkeypatch.setattr(router, "ask", ask)

    models = client.get("/api/local-chat/models", headers=H).json()
    assert models["default_model"] == CODER
    s = client.post("/api/local-chat/sessions", json={"mode": "build", "workspace": str(repo), "cloud_assist": "off"}, headers=H).json()
    frames = []

    async def approve(frame):
        frames.append(frame)
        if frame.get("type") == "local_chat_approval":
            approval.resolve(frame["approval_id"], "once", actor="owner (test)")

    async def go():
        await chat.send(s["id"], "write offline.txt", approve)
        await asyncio.wait_for(chat._running[s["id"]], 30)
    asyncio.run(go())
    assert store.messages(s["id"])[-1]["text"] == "Done offline."
    wt = Path(store.get(s["id"])["worktree"]["path"])
    assert (wt / "offline.txt").read_text(encoding="utf-8") == "works\n"
    assert "offline.txt" in client.get(f"/api/local-chat/sessions/{s['id']}/worktree/diff", headers=H).json()["diff"]
    assert client.post(f"/api/local-chat/sessions/{s['id']}/worktree/commit", json={"message": "offline"}, headers=H).status_code == 200
    assert client.post(f"/api/local-chat/sessions/{s['id']}/send", json={"text": "x", "cloud": True}, headers=H).status_code == 400

    async def plan_model(prompt, **kw):
        return json.dumps({"tasks": [{"id": "a", "title": "A task", "proof": "python -m pytest tests -q"}]}), "m/local"
    llm.set_completer(plan_model)
    try:
        f = store.create(model=CODER, mode="factory", workspace=str(repo))
        out = asyncio.run(factory.preview(f["id"], "do a thing"))
        assert out["plan"]["tasks"][0]["title"] == "A task"
    finally:
        llm.set_completer(None)
    hits = client.get("/api/capabilities?q=local+chat&offline=true", headers=H).json()["results"]
    assert hits and not any(h["network"] or h["cloud"] for h in hits)
    assert offline == [], f"something tried the network: {offline}"



def test_a_hub_that_is_down_or_degraded_is_said_plainly(temp_db, monkeypatch):
    from bot.modelmistress import supervisor
    from bot.router import router

    async def boom(prompt, **kw):
        raise ConnectionError("connection refused")
    monkeypatch.setattr(router, "ask", boom)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": []})
    for hub, expect in (({"up": False}, "ModelMistress (the local model hub) is not running"),
                        ({"up": True, "degraded": "engine crashed"}, "degraded (engine crashed)")):
        monkeypatch.setattr(supervisor, "status", lambda hub=hub: hub)
        s = store.create(model=CODER, mode="chat")

        async def go():
            await chat.send(s["id"], "hi", _noop)
            await asyncio.wait_for(chat._running[s["id"]], 10)
        asyncio.run(go())
        last = store.messages(s["id"])[-1]
        assert last["role"] == "error" and expect in last["text"] and "connection refused" in last["text"]
        assert store.get(s["id"])["status"] == "error"


async def _noop(frame):
    return None


def test_one_gpu_shared_with_a_factory_run(temp_db, client, monkeypatch):
    call, _ = _hub(loaded=())
    monkeypatch.setattr(catalog, "get", lambda refresh=False, ttl_s=None: REAL_BUILD(call, reports=REPORTS, min_context=0))

    class Running:
        def done(self):
            return False
    monkeypatch.setitem(factory._runs, "busy-session", Running())
    monkeypatch.setitem(factory._models, "busy-session", CODER)
    s = store.create(model=SMALL, mode="chat")                         # SMALL's plan unloads CODER (see the stand-in hub)
    r = client.post(f"/api/local-chat/sessions/{s['id']}/send", json={"text": "hi"}, headers=H)
    assert r.status_code == 409 and "Factory run is using" in r.json()["detail"]
    assert client.get("/api/local-chat/models", headers=H).json()["factory_models"] == [CODER]
    chat.gpu_check(CODER)                                               # the Factory's own model is fine
    monkeypatch.delitem(factory._runs, "busy-session")
    chat.gpu_check(SMALL)                                               # and anything once the run is over


def test_the_model_picker_answers_from_its_cache_in_under_200_ms(temp_db, client, monkeypatch):
    call, calls = _hub()
    monkeypatch.setattr(catalog, "build", lambda *a, **k: REAL_BUILD(call, reports=REPORTS, min_context=16384))
    assert client.get("/api/local-chat/models", headers=H).status_code == 200       # warms the cache
    n = len(calls)
    t0 = time.perf_counter()
    for _ in range(5):
        assert client.get("/api/local-chat/models", headers=H).status_code == 200
    per = (time.perf_counter() - t0) / 5
    assert len(calls) == n, "a cached picker must not ask the hub again"
    assert per < 0.2, f"{per * 1000:.0f} ms per call"
