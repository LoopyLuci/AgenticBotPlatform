"""The unit runtime's guarantees 3-8 (docs/runtime.md), with toy units, real FastAPI apps and real sqlite."""
from __future__ import annotations

import asyncio
import re

from runtime_toys import WS, Toy, client, make_engine, manifest, thread_names

from bot.agent_runtime import toolspec
from bot.runtime.manifest import Manifest


def run(coro):
    return asyncio.run(coro)


def test_load_use_unload_reload_leaves_nothing(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        threads = thread_names()
        async with client(eng) as c:
            for _ in range(2):
                assert (await eng.load("alpha", version="1")).ok
                assert (await c.get("/api/alpha/x")).json()["v"] == "1"
                assert "toy_alpha" in toolspec.registered_names()
                assert [p["id"] for p in eng.ui_manifest()["panels"]] == ["alpha-panel"]
                await asyncio.sleep(0.02)
                assert eng.supervisor.total() == 1
                assert (await eng.unload("alpha")).ok
                await eng.settle()
                r = await c.get("/api/alpha/x")
                assert r.status_code == 404
                assert r.json() == {"error": {"code": "unit_unavailable", "unit": "alpha", "state": "unloaded"}}
                assert "toy_alpha" not in toolspec.registered_names()
                assert eng.ui_manifest()["panels"] == []
                assert eng.effective_config() == {}
                assert eng.supervisor.total() == 0 and eng.supervisor.groups() == []
        # nothing the runtime started is left running (a daemon thread another test owns, e.g. a model-hub probe, is not ours)
        # Backend clients (ollama, unsloth ...) start "<name>-find" probe threads when another test on the same worker
        # touches them.
        leaked = {n for n in thread_names() - threads if not re.match(r"[\w.]+-find\b", n)}
        assert not leaked, leaked
    run(go())


def test_swap_under_load_drops_no_request(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            stop = asyncio.Event()
            seen: list = []

            async def hammer():
                while not stop.is_set():
                    r = await c.get("/api/alpha/x")
                    assert r.status_code == 200, r.text
                    seen.append(r.json()["v"])

            workers = [asyncio.create_task(hammer()) for _ in range(8)]
            await asyncio.sleep(0.05)
            report = await eng.swap("alpha", "2")
            await asyncio.sleep(0.05)
            stop.set()
            await asyncio.gather(*workers)
            await eng.settle()
        assert report.ok and eng.generation == 2
        assert seen[0] == "1" and seen[-1] == "2" and len(seen) > 20
        assert set(seen) == {"1", "2"}
    run(go())


def test_inflight_request_finishes_on_old_instance_before_stop(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        Toy.log.clear()
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            eng._active["alpha@default"].unit.delay = 0.2
            slow = asyncio.create_task(c.get("/api/alpha/x"))
            await asyncio.sleep(0.05)
            old = eng._active["alpha@default"]
            assert old.inflight == 1
            await eng.swap("alpha", "2")
            assert ("stop", "alpha@default", "1") not in Toy.log
            assert (await slow).json()["v"] == "1"
            await eng.settle()
            assert ("stop", "alpha@default", "1") in Toy.log
    run(go())


def test_failing_health_leaves_generation_and_behaviour(tmp_path):
    async def go():
        eng = make_engine(tmp_path, units=("alpha", "beta"))
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            gen, mid = eng.generation, eng.manifest.id
            report = await eng.swap("alpha", "2", settings={"health": "failed"})
            assert not report.ok and "alpha@default" in report.failed
            assert eng.generation == gen and eng.manifest.id == mid
            assert (await c.get("/api/alpha/x")).json()["v"] == "1"
            assert eng.supervisor.groups() and all(g.startswith("alpha@default#") for g in eng.supervisor.groups())
            assert len(eng.supervisor.groups()) == 1
            report = await eng.load("beta", version="1", settings={"health": "failed"})
            assert not report.ok
            r = await c.get("/api/beta/x")
            assert r.json()["error"]["state"] == "failed"
            assert eng.generation == gen and "toy_beta" not in toolspec.registered_names()
    run(go())


def test_health_timeout_aborts(tmp_path):
    async def go():
        eng = make_engine(tmp_path, health_timeout=0.05)
        await eng.load("alpha", version="1")

        class Slow(Toy):
            async def health(self, ctx):
                await asyncio.sleep(1)

        eng.factories.register("alpha", Slow, version="2")
        report = await eng.swap("alpha", "2")
        assert not report.ok and "timed out" in report.failed["alpha@default"]
        assert eng.generation == 1
    run(go())


def test_rollback_restores_old_behaviour_and_reuses_standby(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            v1 = eng._active["alpha@default"].unit
            await eng.swap("alpha", "2")
            await eng.settle()
            assert (await c.get("/api/alpha/x")).json()["v"] == "2"
            report = await eng.rollback()
            assert report.ok and eng.generation == 3
            assert (await c.get("/api/alpha/x")).json()["v"] == "1"
            assert eng._active["alpha@default"].unit is v1 and v1.started == 2
            await eng.settle()
    run(go())


def test_two_instances_keep_separate_state_and_swap_independently(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            await eng.load("alpha", instance="bot-7", version="1")
            for _ in range(3):
                await c.get("/api/alpha/x")
            assert (await c.get("/api/alpha@bot-7/x")).json()["n"] == 1
            assert (await c.get("/api/alpha/x")).json()["n"] == 4
            assert "toy_alpha" in toolspec.registered_names() and "toy_alpha@bot-7" in toolspec.registered_names()
            await eng.swap("alpha", "2", instance="bot-7")
            await eng.settle()
            assert (await c.get("/api/alpha@bot-7/x")).json() == {"v": "2", "n": 2, "instance": "bot-7"}
            assert (await c.get("/api/alpha/x")).json()["v"] == "1"
    run(go())


def test_state_carries_over_a_swap(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        await eng.load("alpha", version="1")
        eng._active["alpha@default"].unit.memory = 42
        await eng.swap("alpha", "2")
        assert eng._active["alpha@default"].unit.memory == 42
    run(go())


def test_branch_switch_is_all_or_nothing(tmp_path):
    async def go():
        eng = make_engine(tmp_path, units=("alpha", "beta"))
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            eng.fork("try")
            await eng.swap("alpha", "2", branch="try")
            await eng.load("beta", version="1", settings={"health": "failed"}, branch="try")
            assert (await c.get("/api/alpha/x")).json()["v"] == "1"        # editing a branch changes nothing live
            plan = eng.plan(eng.get_branch("try"))
            assert [o.action for o in plan.ops] == ["swap", "load"]
            report = await eng.switch("try")
            assert not report.ok and eng.generation == 1
            assert (await c.get("/api/alpha/x")).json()["v"] == "1"
            await eng.load("beta", version="1", branch="try")
            assert (await eng.switch("try")).ok and eng.generation == 2
            assert (await c.get("/api/alpha/x")).json()["v"] == "2"
            assert (await c.get("/api/beta/x")).status_code == 200
            assert eng.branches.get("previous").instances.keys() == {"alpha@default"}
            assert (await eng.rollback()).ok
            assert (await c.get("/api/beta/x")).status_code == 404
    run(go())


def test_shadow_branch_does_not_change_live(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            await c.get("/api/alpha/x")
            eng.fork("try")
            await eng.swap("alpha", "2", branch="try")
            gen = eng.generation
            assert (await eng.preview("try")).ok
            assert (await c.get("/api/_branch/try/alpha/x")).json() == {"v": "2", "n": 2, "instance": "default"}
            assert (await c.get("/api/_branch/try/alpha/x")).json()["n"] == 3     # copy-on-write: its own counter
            live = (await c.get("/api/alpha/x")).json()
            assert live == {"v": "1", "n": 2, "instance": "default"}              # live state untouched
            assert eng.generation == gen and "toy_alpha" in toolspec.registered_names()
            assert eng.ui_manifest()["panels"][0]["version"] == "1"
            await eng.end_preview("try")
            assert (await c.get("/api/_branch/try/alpha/x")).status_code == 404
            assert eng.store.namespace("alpha@default#try").keys() == []
    run(go())


def test_staged_instance_is_reachable_only_under_staged_prefix(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        async with client(eng) as c:
            await eng.load("alpha", version="1")
            seen = {}

            class Probe(Toy):
                async def health(self, ctx):
                    seen["staged"] = (await c.get("/api/_staged/alpha/x")).json()["v"]
                    seen["live"] = (await c.get("/api/alpha/x")).json()["v"]
                    return await super().health(ctx)

            eng.factories.register("alpha", Probe, version="2")
            assert (await eng.swap("alpha", "2")).ok
            assert seen == {"staged": "2", "live": "1"}
    run(go())


def test_websocket_stays_on_the_instance_that_accepted_it(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        Toy.log.clear()
        await eng.load("alpha", version="1")
        sock = WS(eng.dispatcher, "/api/alpha/ws")
        await sock.open()
        assert await sock.ask() == "1"
        await eng.swap("alpha", "2")
        async with client(eng) as c:
            assert (await c.get("/api/alpha/x")).json()["v"] == "2"
        assert await sock.ask() == "1"                       # still the old instance
        await asyncio.sleep(0.05)
        assert ("stop", "alpha@default", "1") not in Toy.log  # drain waits for the connection
        await sock.close()
        await eng.settle()
        assert ("stop", "alpha@default", "1") in Toy.log
        late = WS(eng.dispatcher, "/api/alpha/ws")
        await late.open()
        assert await late.ask() == "2"
        await late.close()
    run(go())


def test_events_and_on_commit(tmp_path):
    async def go():
        seen: list = []
        eng = make_engine(tmp_path, on_commit=seen.append)
        await eng.load("alpha", version="1")
        await eng.swap("alpha", "2")
        await eng.unload("alpha")
        assert [e["generation"] for e in seen] == [1, 2, 3]
        assert seen[0]["added"] == ["alpha@default"] and seen[1]["changed"] == ["alpha@default"]
        assert seen[2]["removed"] == ["alpha@default"]
        assert [e["generation"] for e in eng.events_since(1)] == [2, 3]
        await eng.settle()
    run(go())


def test_boot_skips_a_failing_unit_and_commit_queue_serialises(tmp_path):
    async def go():
        eng = make_engine(tmp_path, units=("alpha", "beta"))
        target = manifest(alpha=("1",), beta=("1", {"health": "failed"}))
        report = await eng.boot(target)
        assert report.ok and list(eng.manifest.instances) == ["alpha@default"]
        assert eng.status()["units"][0]["ref"] == "alpha@default"
        reports = await asyncio.gather(eng.swap("alpha", "2"), eng.swap("alpha", "3"), eng.swap("alpha", "4"))
        assert all(r.ok for r in reports) and eng.generation == 4
        assert eng.manifest.instances["alpha@default"].version == "4"
        await eng.settle()
        assert Manifest.from_dict(eng.manifest.to_dict()).id == eng.manifest.id
    run(go())
