"""SentinelBoard against the REAL AgentSentinel daemon (the wire format the fake server in coordination_fakes.py can only
imitate). Needs a checkout of AgentSentinel with its node_modules (tsx) installed, named by ABP_TEST_AGENT_SENTINEL_REPO,
and node; skipped otherwise. The daemon runs on a temp folder and free ports and is always terminated."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import httpx
import pytest

from bot.coordination import BoardError, SentinelBoard, WorkLost, WorkSession, current_work

REPO = Path(os.environ["ABP_TEST_AGENT_SENTINEL_REPO"]) if os.environ.get("ABP_TEST_AGENT_SENTINEL_REPO") else None
SERVER = REPO / "agent-daemon" / "src" / "server.ts" if REPO else None
ADMIN = "live-admin"
AGENT = "live-agent"

pytestmark = pytest.mark.skipif(
    REPO is None or shutil.which("node") is None or not (REPO / "node_modules" / "tsx").exists() or not SERVER.exists(),
    reason="set ABP_TEST_AGENT_SENTINEL_REPO to an AgentSentinel checkout with node and its npm install (tsx)",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def daemon():
    base = Path(tempfile.mkdtemp(prefix="abp-sentinel-live-"))
    port = free_port()
    env = {
        **os.environ,
        "HTTP_PORT": str(port),
        "AGENT_SENTINEL_ENDPOINT": f"tcp://127.0.0.1:{free_port()}",
        "API_TOKEN": ADMIN,
        "AGENT_TOKEN": AGENT,
        "SQLITE_PATH": str(base / "sentinel.db"),
        "OPS_DB_PATH": str(base / "ops.db"),
        "BLOB_DIR": str(base / "blobs"),
        "QUARANTINE_DIR": str(base / "quarantine"),
        "STORAGE_FS_DIR": str(base / "snapshots"),
    }
    log = open(base / "daemon.log", "wb")
    proc = subprocess.Popen(["node", "--import", "tsx", str(SERVER)], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while True:
            if proc.poll() is not None:
                pytest.fail("daemon exited: " + (base / "daemon.log").read_text(errors="replace")[-2000:])
            try:
                req = urllib.request.Request(url + "/api/v1/status", headers={"Authorization": f"Bearer {ADMIN}"})
                urllib.request.urlopen(req, timeout=2).read()
                break
            except OSError:
                if time.time() > deadline:
                    pytest.fail("daemon did not start: " + (base / "daemon.log").read_text(errors="replace")[-2000:])
                time.sleep(0.3)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def board(daemon):
    b = SentinelBoard(daemon, AGENT, timeout=15)
    yield b
    b.close()


@pytest.fixture
def operator(daemon):
    b = SentinelBoard(daemon, ADMIN, timeout=15)
    yield b
    b.close()


@pytest.fixture
def name():
    return f"live/{uuid.uuid4().hex[:8]}"


def code(fn, *a, **kw):
    with pytest.raises(BoardError) as ei:
        fn(*a, **kw)
    return ei.value


def test_the_daemon_answers_the_probe(board):
    assert board.ping() is True


def test_create_and_dedupe(board, name):
    first, d1 = board.create_task(name, "build it", key="k", priority=3, scope={"resources": ["path:abp:bot/db.py"]})
    again, d2 = board.create_task(name, "build it again", key="k")
    assert not d1 and d2 and again["id"] == first["id"]
    assert first["state"] == "open" and first["scope"]["resources"] == ["path:abp:bot/db.py"]
    got = board.get_task(first["id"])
    assert got["title"] == "build it" and got["version"] >= 1
    assert [t["id"] for t in board.list_tasks(name)] == [first["id"]]
    claim = board.claim(first["id"], "w")
    board.set_state(first["id"], "w", claim["fence"], "done", result={"v": 1})
    fresh, d3 = board.create_task(name, "x", key="k", created_by="boss")
    assert not d3 and fresh["id"] != first["id"]
    board.cancel(fresh["id"], agent="boss")
    same, d4 = board.create_task(name, "x", key="k", dedupe_done=True)
    assert d4 and same["id"] == first["id"]


def test_next_with_deps_needs_locks_and_the_hint(board, name):
    a = board.create_task(name, "A")[0]
    b = board.create_task(name, "B needs A", deps=[a["id"]])[0]
    c = board.create_task(name, "C needs gpu", needs=["gpu"])[0]
    got = board.next(name, "w1", capabilities=["x"], lease_s=30)
    assert got["task"]["id"] == a["id"] and got["task"]["owner"] == "w1" and got["fence"] > 0
    assert board.next(name, "w2") is None
    hint = board.last_hint
    assert hint["blocked_by_deps"] == 1 and hint["needs_capabilities"] == 1
    board.set_state(a["id"], "w1", got["fence"], "done")
    assert board.next(name, "w2")["task"]["id"] == b["id"]
    assert board.next(name, "w2", capabilities=["gpu"])["task"]["id"] == c["id"]


def test_next_skips_tasks_whose_files_are_locked(board, name):
    res = f"path:{uuid.uuid4().hex[:6]}:src"
    holder = board.create_task(name, "holder", scope={"resources": [res]})[0]
    blocked = board.create_task(name, "wants a file inside", scope={"resources": [res + "/x.py"]})[0]
    board.claim(holder["id"], "h", lease_s=30)
    assert board.next(name, "w") is None
    assert board.last_hint["blocked_by_locks"] == 1
    e = code(board.claim, blocked["id"], "w")
    assert e.code == "LOCKED" and e.holders and e.holders[0]["agent"] == "h"


def test_claim_errors(board, name):
    a = board.create_task(name, "A")[0]
    b = board.create_task(name, "B", deps=[a["id"]])[0]
    assert code(board.claim, b["id"], "w").code == "DEPS"
    assert code(board.claim, a["id"], "w", expect_version=a["version"] + 9).code == "VERSION"
    board.claim(a["id"], "w1", lease_s=30)
    assert code(board.claim, a["id"], "w2").code == "TAKEN"
    assert code(board.get_task, str(uuid.uuid4())).code == "NOT_FOUND"


def test_two_concurrent_claims_have_one_winner(board, name):
    t = board.create_task(name, "contended")[0]
    results: list = []
    gate = threading.Barrier(8)

    def go(i):
        gate.wait()
        try:
            board.claim(t["id"], f"w{i}", lease_s=30)
            results.append("won")
        except BoardError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert results.count("won") == 1 and results.count("TAKEN") == 7


def test_acquire_is_all_or_nothing_with_holders(board, name):
    repo = uuid.uuid4().hex[:6]
    t = board.create_task(name, "rewrite parser")[0]
    claim = board.claim(t["id"], "a", lease_s=30)
    got = board.acquire("a", [f"path:{repo}:a.py"], task=t["id"], lease_s=30)
    assert got["fence"] > claim["fence"] and got["locks"][0]["resource"] == f"path:{repo}:a.py"
    e = code(board.acquire, "b", [f"path:{repo}:b.py", f"path:{repo}:a.py"], lease_s=30)
    assert e.code == "LOCKED"
    h = e.holders[0]
    assert h["agent"] == "a" and h["task"] == t["id"] and h["title"] == "rewrite parser"
    assert board.list_locks(owner="b") == []


def test_path_overlap(board):
    repo = uuid.uuid4().hex[:6]
    board.acquire("a", [f"path:{repo}:bot"], lease_s=30)
    assert code(board.acquire, "b", [f"path:{repo}:bot/db.py"]).code == "LOCKED"
    board.acquire("c", [f"path:{repo}:botanical.txt"], lease_s=30)
    board.acquire("a", [f"path:{repo}:bot/db"], lease_s=30)
    assert code(board.acquire, "d", [f"path:{repo}:bot/db"]).code == "LOCKED"
    shared = uuid.uuid4().hex[:6]
    board.acquire("a", [{"resource": f"repo:{shared}", "mode": "shared"}], lease_s=30)
    board.acquire("b", [{"resource": f"repo:{shared}", "mode": "shared"}], lease_s=30)
    assert code(board.acquire, "c", [f"repo:{shared}"]).code == "LOCKED"


def test_heartbeat_lock_release_and_done_frees_locks(board, name):
    res = f"custom:{uuid.uuid4().hex[:6]}"
    t = board.create_task(name, "work", scope={"resources": [res]})[0]
    claim = board.claim(t["id"], "a", lease_s=30)
    assert [lk["resource"] for lk in claim["locks"]] == [res]
    hb = board.heartbeat(t["id"], "a", claim["fence"], lease_s=30, progress="halfway")
    assert hb["lease_until"] > 0
    assert board.get_task(t["id"])["note"] == "halfway"
    assert board.list_locks(resource_prefix=res)
    board.set_state(t["id"], "a", claim["fence"], "running")
    assert board.get_task(t["id"])["state"] == "running"
    board.set_state(t["id"], "a", claim["fence"], "done", result={"ok": True})
    assert board.list_locks(resource_prefix=res) == []
    assert board.get_task(t["id"])["result"] == {"ok": True}
    adhoc = board.acquire("z", [f"custom:{uuid.uuid4().hex[:6]}"], lease_s=30)
    board.lock_heartbeat("z", adhoc["fence"], lease_s=30)
    board.release_locks("z", adhoc["fence"])
    assert board.list_locks(owner="z") == []


def test_a_stale_fence_after_lease_expiry_and_reclaim(board, name):
    t = board.create_task(name, "contested")[0]
    old = board.claim(t["id"], "slow", lease_s=5)
    time.sleep(6)
    new = board.claim(t["id"], "fast", lease_s=30)         # the expired claim was requeued, so this one is allowed
    assert new["fence"] > old["fence"]
    assert code(board.heartbeat, t["id"], "slow", old["fence"]).code == "STALE_FENCE"
    assert code(board.set_state, t["id"], "slow", old["fence"], "done").code == "STALE_FENCE"
    assert code(board.heartbeat, t["id"], "other", new["fence"]).code == "NOT_OWNER"
    after = board.get_task(t["id"])
    assert after["owner"] == "fast" and after["attempts"] == 1


def test_a_work_session_loses_a_taken_over_task(board, name):
    import asyncio

    async def scenario():
        t = board.create_task(name, "contested")[0]
        with pytest.raises(WorkLost):
            async with await WorkSession.claim(board, t["id"], "slow", lease_s=5):
                # Stand in for a stalled process: no heartbeats, so the lease runs out and another agent takes the task;
                # then the process wakes up and its next heartbeat is refused.
                work = current_work.get()
                work._beat_task.cancel()
                await asyncio.sleep(5.5)
                await asyncio.to_thread(board.claim, t["id"], "fast", lease_s=30)
                work._beat_task = asyncio.create_task(work._beat())
                await asyncio.sleep(6)

    asyncio.run(scenario())


def test_summary_and_presence(board, operator, name):
    a = board.create_task(name, "work")[0]
    board.create_task(name, "later", deps=[a["id"]])
    board.presence("idler", "idle", capabilities=["gpu"])
    board.next(name, "worker", lease_s=30)
    s = operator.summary(name)
    b = s["boards"][name]
    assert b["counts"]["claimed"] == 1 and b["counts"]["open"] == 1
    assert b["blocked"][0]["reason"] == "deps"
    assert "idler" in s["agents"]["idle"]
    assert "idler" in {p["agent"] for p in board.list_presence()}
    assert name in operator.summary()["boards"]
    assert code(board.summary).status == 403            # across all boards is operator-only


def test_an_agent_token_must_match_the_agent(daemon, board, name):
    issued = httpx.post(daemon + "/api/v1/agent-tokens", json={"agentId": "alice"}, headers={"Authorization": f"Bearer {ADMIN}"}).json()
    mine = SentinelBoard(daemon, issued["token"], timeout=10)
    try:
        t = mine.create_task(name, "alice's", created_by="alice")[0]
        assert mine.claim(t["id"], "alice", lease_s=30)["task"]["owner"] == "alice"
        e = code(mine.claim, board.create_task(name, "other")[0]["id"], "mallory")
        assert e.status == 403 and e.code == "AGENT_MISMATCH"
    finally:
        mine.close()


def test_a_wrong_token_is_unauthorized(daemon):
    bad = SentinelBoard(daemon, "nope")
    try:
        assert bad.ping() is False
        e = code(bad.list_presence)
        assert e.status in (401, 403) and e.code in ("FORBIDDEN", "UNAUTHORIZED")
    finally:
        bad.close()
