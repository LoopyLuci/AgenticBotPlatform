"""The work board protocol, one suite for both implementations: LocalBoard (ABP's own SQLite) and SentinelBoard talking
to a fake AgentSentinel over real HTTP. Time is a clock the tests move, so leases expire on command."""
from __future__ import annotations

import asyncio
import threading

import pytest

from bot import coordination
from bot.coordination import BoardError, LocalBoard, SentinelBoard, Unavailable, WorkLost, WorkSession, conflicts
from coordination_fakes import TOKEN, FakeClock, FakeSentinel


@pytest.fixture(params=["local", "sentinel"])
def env(request, tmp_path):
    clock = FakeClock()
    backing = LocalBoard(tmp_path / f"{request.param}.db", clock=clock, min_lease_s=0.01)
    if request.param == "local":
        yield backing, clock
        backing.close()
        return
    fake = FakeSentinel(backing)
    client = SentinelBoard(fake.url, TOKEN, timeout=5)
    yield client, clock
    client.close()
    fake.close()
    backing.close()


@pytest.fixture
def board(env):
    return env[0]


@pytest.fixture
def clock(env):
    return env[1]


def mk(board, title, **kw):
    task, _ = board.create_task("b", title, **kw)
    return task


def code(fn, *a, **kw):
    with pytest.raises(BoardError) as ei:
        fn(*a, **kw)
    return ei.value


# ---------------------------------------------------------------- dedupe
def test_same_key_returns_the_existing_task(board):
    first, deduped = board.create_task("b", "one", key="k1")
    again, deduped2 = board.create_task("b", "one, again", key="k1")
    assert not deduped and deduped2 and again["id"] == first["id"]
    other_board, d3 = board.create_task("c", "one", key="k1")
    assert not d3 and other_board["id"] != first["id"]


def test_a_finished_task_is_only_matched_with_dedupe_done(board):
    t = mk(board, "x", key="k")
    got = board.claim(t["id"], "a", lease_s=60)
    board.set_state(t["id"], "a", got["fence"], "done", result={"v": 1})
    fresh, deduped = board.create_task("b", "x", key="k")
    assert not deduped and fresh["id"] != t["id"]
    board.cancel(fresh["id"])
    same, deduped = board.create_task("b", "x", key="k", dedupe_done=True)
    assert deduped and same["id"] == t["id"] and same["result"] == {"v": 1}


# ------------------------------------------------------------------ next
def test_next_picks_by_priority_then_age_and_claims(board):
    low = mk(board, "low")
    high = mk(board, "high", priority=5)
    old = mk(board, "old, same priority as low")
    first = board.next("b", "w1")
    assert first["task"]["id"] == high["id"] and first["task"]["owner"] == "w1" and first["task"]["state"] == "claimed"
    assert board.next("b", "w2")["task"]["id"] == low["id"]
    assert board.next("b", "w3")["task"]["id"] == old["id"]
    assert board.next("b", "w4") is None


def test_next_respects_deps_needs_and_locks(board):
    a = mk(board, "A")
    b = mk(board, "B needs A", deps=[a["id"]])
    c = mk(board, "C needs gpu", needs=["gpu"])
    d = mk(board, "D edits a file", scope={"resources": ["path:ws:src/x.py"]})
    got = board.next("b", "w1")                      # A: B waits for it, C needs a capability, D is free but younger
    assert got["task"]["id"] == a["id"]
    got_d = board.next("b", "w2")
    assert got_d["task"]["id"] == d["id"] and got_d["locks"][0]["resource"] == "path:ws:src/x.py"
    assert board.next("b", "w3") is None             # B blocked by deps, C by capability
    board.set_state(a["id"], "w1", got["fence"], "done")
    assert board.next("b", "w3")["task"]["id"] == b["id"]
    assert board.next("b", "w3", capabilities=["gpu"])["task"]["id"] == c["id"]


def test_next_skips_a_task_whose_files_are_locked(board):
    holder = mk(board, "holder", scope={"resources": ["path:ws:src"]})
    blocked = mk(board, "wants a file inside", scope={"resources": ["path:ws:src/x.py"]})
    free = mk(board, "free")
    board.claim(holder["id"], "h", lease_s=60)
    got = board.next("b", "w")
    assert got["task"]["id"] == free["id"]
    assert board.next("b", "w") is None
    e = code(board.claim, blocked["id"], "w")
    assert e.code == "LOCKED" and e.holders[0]["agent"] == "h" and e.holders[0]["title"] == "holder"


def test_claim_errors_are_taken_deps_version(board):
    a = mk(board, "A")
    b = mk(board, "B", deps=[a["id"]])
    assert code(board.claim, b["id"], "w").code == "DEPS"
    assert code(board.claim, a["id"], "w", expect_version=a["version"] + 5).code == "VERSION"
    board.claim(a["id"], "w1", lease_s=60)
    assert code(board.claim, a["id"], "w2").code == "TAKEN"
    assert code(board.get_task, "nope").code == "NOT_FOUND"


def test_a_claim_race_has_exactly_one_winner(board):
    t = mk(board, "contended")
    results: list = []
    gate = threading.Barrier(8)

    def go(i):
        gate.wait()
        try:
            board.claim(t["id"], f"w{i}", lease_s=60)
            results.append("won")
        except BoardError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert results.count("won") == 1 and results.count("TAKEN") == 7


def test_next_race_hands_each_task_to_one_worker(board):
    for i in range(5):
        mk(board, f"t{i}")
    got: list = []
    gate = threading.Barrier(8)

    def go(i):
        gate.wait()
        c = board.next("b", f"w{i}", lease_s=60)
        if c:
            got.append(c["task"]["id"])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(got) == 5 and len(set(got)) == 5


# ----------------------------------------------------------------- locks
def test_acquire_is_all_or_nothing_and_names_the_holder(board):
    t = mk(board, "rewrite parser")
    got = board.claim(t["id"], "a", lease_s=60)
    board.acquire("a", ["path:ws:a.py"], task=t["id"], lease_s=60)
    e = code(board.acquire, "b", ["path:ws:b.py", "path:ws:a.py"], lease_s=60)
    assert e.code == "LOCKED"
    h = e.holders[0]
    assert h["agent"] == "a" and h["task"] == t["id"] and h["title"] == "rewrite parser" and h["resource"] == "path:ws:a.py"
    assert board.list_locks(owner="b") == []         # b.py was not taken either
    assert got["fence"] > 0


def test_the_path_overlap_rule(board):
    board.acquire("a", ["path:abp:bot"], lease_s=60)
    assert code(board.acquire, "b", ["path:abp:bot/db.py"]).code == "LOCKED"
    board.acquire("b", ["path:abp:botanical.txt"], lease_s=60)       # a sibling name, not a child
    board.acquire("c", ["path:other:bot/db.py"], lease_s=60)         # another repo
    board.acquire("a", ["path:abp:bot/db"], lease_s=60)
    board.acquire("d", ["port:80"], lease_s=60)
    assert code(board.acquire, "e", ["port:80"]).code == "LOCKED"


def test_conflicts_function():
    assert conflicts("path:abp:bot", "path:abp:bot/db.py")
    assert not conflicts("path:abp:bot/db", "path:abp:bot/db.py")
    assert conflicts("path:abp:bot/db.py", "path:abp:bot/db.py")
    assert not conflicts("path:abp:a", "path:xyz:a")
    assert conflicts("model:m1", "model:m1") and not conflicts("model:m1", "model:m2")
    assert not conflicts("repo:abp", "path:abp:x")


def test_shared_locks_coexist_but_exclusive_conflicts(board):
    board.acquire("a", [{"resource": "repo:abp", "mode": "shared"}], lease_s=60)
    board.acquire("b", [{"resource": "repo:abp", "mode": "shared"}], lease_s=60)
    assert code(board.acquire, "c", ["repo:abp"]).code == "LOCKED"


def test_lock_heartbeat_release_and_fence(board):
    got = board.acquire("a", ["custom:x"], lease_s=60)
    board.lock_heartbeat("a", got["fence"], lease_s=60)
    assert code(board.lock_heartbeat, "a", got["fence"] + 99).code == "STALE_FENCE"
    board.release_locks("a", got["fence"])
    assert board.list_locks() == []
    board.acquire("b", ["custom:x"], lease_s=60)


def test_a_lock_expires_without_heartbeat(board, clock):
    board.acquire("a", ["custom:x"], lease_s=1)
    clock.advance(5)
    assert board.list_locks() == []
    board.acquire("b", ["custom:x"], lease_s=60)


# ---------------------------------------------------------------- leases
def test_expired_lease_requeues_then_fails_after_max_attempts(board, clock):
    t = mk(board, "flaky", max_attempts=2, scope={"resources": ["custom:r"]})
    board.claim(t["id"], "a", lease_s=1)
    assert [lk["resource"] for lk in board.list_locks()] == ["custom:r"]
    clock.advance(2)
    again = board.get_task(t["id"])
    assert again["state"] == "open" and again["attempts"] == 1 and again["owner"] is None
    assert board.list_locks() == []                  # the dead owner's locks went with the lease
    board.claim(t["id"], "b", lease_s=1)
    clock.advance(2)
    final = board.get_task(t["id"])
    assert final["state"] == "failed" and "lease expired 2 times" in final["note"]


def test_heartbeat_keeps_a_lease_and_its_locks_alive(board, clock):
    t = mk(board, "long", scope={"resources": ["custom:r"]})
    got = board.claim(t["id"], "a", lease_s=3)
    for _ in range(4):
        clock.advance(2)
        board.heartbeat(t["id"], "a", got["fence"], lease_s=3)
    assert board.get_task(t["id"])["state"] == "claimed" and board.list_locks()


def test_a_stale_fence_is_refused(board, clock):
    t = mk(board, "contested")
    old = board.claim(t["id"], "slow", lease_s=1)
    clock.advance(2)
    new = board.claim(t["id"], "fast", lease_s=60)
    assert new["fence"] > old["fence"]
    assert code(board.heartbeat, t["id"], "slow", old["fence"]).code == "STALE_FENCE"
    assert code(board.set_state, t["id"], "slow", old["fence"], "done", result={"late": True}).code == "STALE_FENCE"
    assert code(board.heartbeat, t["id"], "other", new["fence"]).code == "NOT_OWNER"
    done = board.set_state(t["id"], "fast", new["fence"], "done", result={"ok": True})
    assert done["state"] == "done" and board.get_task(t["id"])["result"] == {"ok": True}


def test_states_released_and_failed_free_locks(board):
    t = mk(board, "x", scope={"resources": ["custom:r"]})
    got = board.claim(t["id"], "a", lease_s=60)
    board.set_state(t["id"], "a", got["fence"], "running")
    assert board.get_task(t["id"])["state"] == "running"
    board.set_state(t["id"], "a", got["fence"], "released", note="giving up")
    freed = board.get_task(t["id"])
    assert freed["state"] == "open" and freed["owner"] is None and board.list_locks() == []
    got2 = board.claim(t["id"], "b", lease_s=60)
    failed = board.set_state(t["id"], "b", got2["fence"], "failed", note="broke")
    assert failed["state"] == "failed" and failed["note"] == "broke"


def test_cancel_frees_the_task_and_is_idempotent(board):
    t = mk(board, "x", scope={"resources": ["custom:r"]})
    board.claim(t["id"], "a", lease_s=60)
    assert board.cancel(t["id"], agent="boss")["state"] == "cancelled"
    assert board.list_locks() == []
    assert board.cancel(t["id"])["state"] == "cancelled"


# --------------------------------------------------------- presence/summary
def test_presence_and_summary(board, clock):
    t = mk(board, "work")
    blocked = mk(board, "later", deps=[t["id"]])
    board.presence("idler", "idle", capabilities=["gpu"])
    got = board.next("b", "worker", capabilities=["x"], lease_s=60)
    assert got["task"]["id"] == t["id"]
    s = board.summary("b")
    b = s["boards"]["b"]
    assert b["counts"]["claimed"] == 1 and b["counts"]["open"] == 1
    assert [x["task"] for x in b["blocked"]] == [blocked["id"]] and b["blocked"][0]["reason"] == "deps"
    assert "idler" in s["agents"]["idle"]
    clock.advance(60)                                # nobody has been heard from for far over 3 intervals
    agents = {a["agent"]: a for a in board.list_presence()}
    assert agents["idler"]["gone"] is True


def test_listing_tasks_filters(board):
    a, b = mk(board, "a"), mk(board, "b")
    board.claim(a["id"], "w", lease_s=60)
    assert [t["id"] for t in board.list_tasks("b", state="open")] == [b["id"]]
    assert [t["id"] for t in board.list_tasks("b", owner="w")] == [a["id"]]


# ------------------------------------------------------------ WorkSession
def test_a_work_session_ends_the_task_whatever_happens(board):
    async def scenario():
        ok = mk(board, "ok")
        async with await WorkSession.claim(board, ok["id"], "a", lease_s=30) as w:
            w.result = {"answer": 42}
        assert board.get_task(ok["id"])["state"] == "done" and board.get_task(ok["id"])["result"] == {"answer": 42}

        boom = mk(board, "boom")
        with pytest.raises(RuntimeError):
            async with await WorkSession.claim(board, boom["id"], "a", lease_s=30):
                raise RuntimeError("kaput")
        t = board.get_task(boom["id"])
        assert t["state"] == "open" and "kaput" in t["note"]          # released for someone else

        cancelled = mk(board, "cancelled")

        async def work():
            async with await WorkSession.claim(board, cancelled["id"], "a", lease_s=30):
                await asyncio.sleep(10)

        task = asyncio.ensure_future(work())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert board.get_task(cancelled["id"])["state"] == "open"

        bad = mk(board, "bad")
        async with await WorkSession.claim(board, bad["id"], "a", lease_s=30) as w:
            w.fail("could not", {"why": "x"})
        assert board.get_task(bad["id"])["state"] == "failed"
        assert board.get_task(bad["id"])["note"] == "could not"

    asyncio.run(scenario())


def test_a_session_heartbeats_and_drops_work_it_lost(board, clock):
    async def scenario():
        t = mk(board, "contested", scope={"resources": ["custom:r"]})
        with pytest.raises(WorkLost):
            async with await WorkSession.claim(board, t["id"], "slow", lease_s=0.3) as w:
                clock.advance(5)                                           # its lease runs out ...
                board.claim(t["id"], "fast", lease_s=60)                   # ... and someone else takes the task
                await asyncio.sleep(2)                                     # the next heartbeat (every 0.1 s) must cancel this
                w.result = "must never be recorded"
        cur = board.get_task(t["id"])
        assert cur["owner"] == "fast" and cur["state"] == "claimed" and cur["result"] is None

    asyncio.run(scenario())


def test_session_locks_follow_the_task(board):
    async def scenario():
        t = mk(board, "editor")
        async with await WorkSession.claim(board, t["id"], "a", lease_s=30) as w:
            await w.acquire(["path:ws:a.py"])
            assert code(board.acquire, "b", ["path:ws:a.py"]).code == "LOCKED"
        assert board.list_locks() == []

    asyncio.run(scenario())


# ------------------------------------------------- the sentinel client only
def test_sentinel_auth_and_unreachable(tmp_path):
    backing = LocalBoard(tmp_path / "s.db", min_lease_s=0.01)
    fake = FakeSentinel(backing)
    try:
        bad = SentinelBoard(fake.url, "wrong")
        assert bad.ping() is False
        assert code(bad.list_presence).code == "UNAUTHORIZED"
        bad.close()
    finally:
        fake.close()
    gone = SentinelBoard(fake.url, TOKEN, timeout=1)
    assert gone.ping() is False
    with pytest.raises(Unavailable):
        gone.list_presence()
    gone.close()
    backing.close()


def test_sentinel_hint_travels_in_a_header(tmp_path):
    backing = LocalBoard(tmp_path / "s.db", min_lease_s=0.01)
    fake = FakeSentinel(backing)
    try:
        client = SentinelBoard(fake.url, TOKEN)
        a = client.create_task("b", "A")[0]
        client.create_task("b", "B", deps=[a["id"]])
        client.next("b", "w")
        assert client.next("b", "w") is None
        assert client.last_hint["blocked_by_deps"] == 1
        client.close()
    finally:
        fake.close()
        backing.close()


# ------------------------------------------------------------- the factory
def test_the_factory_prefers_a_live_sentinel_and_falls_back_to_local(monkeypatch, tmp_path):
    monkeypatch.delenv("ABP_COORDINATION_BACKEND", raising=False)
    coordination.reset()
    backing = LocalBoard(tmp_path / "s.db", min_lease_s=0.01)
    fake = FakeSentinel(backing)
    try:
        monkeypatch.setenv("AGENT_SENTINEL_URL", fake.url)
        monkeypatch.setenv("AGENT_SENTINEL_TOKEN", TOKEN)
        assert coordination.get_board().name == "sentinel"
        monkeypatch.setenv("ABP_COORDINATION_BACKEND", "local")
        assert coordination.get_board().name == "local"
    finally:
        fake.close()
    monkeypatch.delenv("ABP_COORDINATION_BACKEND", raising=False)
    coordination.reset()                              # the cached "it answers" must not outlive the server
    assert coordination.get_board().name == "local"
    coordination.reset()


def test_sentinel_url_and_token_come_from_this_machines_agentsentinel_config(tmp_path, monkeypatch):
    from bot.coordination import sentinel

    (tmp_path / "config.json").write_text('{"http_port": 18123, "agent_token": "tok-from-config", "admin_token": "x"}',
                                          encoding="utf-8")
    monkeypatch.setenv("AGENT_SENTINEL_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_SENTINEL_URL", raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_TOKEN", raising=False)
    assert sentinel.sentinel_url() == "http://127.0.0.1:18123"
    assert sentinel.sentinel_token() == "tok-from-config"          # the agent token, never the admin one
    monkeypatch.setenv("AGENT_SENTINEL_TOKEN", "from-env")
    assert sentinel.sentinel_token() == "from-env"                # the environment wins
    monkeypatch.setenv("AGENT_SENTINEL_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("AGENT_SENTINEL_TOKEN")
    assert sentinel.sentinel_token() == "" and sentinel.sentinel_url() == sentinel.DEFAULT_URL
