"""An optional unit (an external MCP server) fails alone; core units still abort a transaction (docs/runtime.md)."""
from __future__ import annotations

import asyncio

from runtime_toys import Toy, make_engine, manifest

from bot.runtime.manifest import InstanceSpec


def run(coro):
    return asyncio.run(coro)


class Unreachable(Toy):
    attempts = 0

    async def start(self, ctx):
        Unreachable.attempts += 1
        if Unreachable.attempts < self.spec.settings.get("works_on", 10 ** 9):
            raise RuntimeError("MCPError: Connection closed")
        await super().start(ctx)


def _engine(tmp_path, **kw):
    eng = make_engine(tmp_path, units=("alpha", "beta"), **kw)
    eng.factories.register_prefix("opt:", Toy, optional=True)
    eng.factories.register("opt:down", Unreachable)
    Unreachable.attempts = 0
    return eng


def test_unreachable_optional_unit_does_not_abort_the_rest(tmp_path):
    async def go():
        eng = _engine(tmp_path, retry_base=3600.0)
        target = manifest(alpha=("1",), beta=("1",)).with_instance("opt:down", "default", InstanceSpec("opt:down", "1", {}))
        report = await eng.apply(target, label="boot")
        assert report.ok and "opt:down@default" in report.degraded
        assert "Connection closed" in report.degraded["opt:down@default"]
        assert eng.generation == 1
        assert {u["ref"] for u in eng.status()["units"]} == {"alpha@default", "beta@default"}
        assert "opt:down@default" in eng.status()["failed"] and eng.status()["degraded"] == ["opt:down@default"]
        assert "opt:down@default" not in eng.manifest.instances
        assert "opt:down@default" in eng.branches.get("live").instances        # still wanted on disk
        await eng.shutdown()
    run(go())


def test_optional_health_failure_is_tolerated(tmp_path):
    async def go():
        eng = _engine(tmp_path, retry_base=3600.0)
        target = manifest(alpha=("1",)).with_instance("opt:one", "default", InstanceSpec("opt:one", "1", {"health": "failed"}))
        report = await eng.apply(target)
        assert report.ok and list(report.degraded) == ["opt:one@default"]
        assert [u["ref"] for u in eng.status()["units"]] == ["alpha@default"]
        await eng.shutdown()
    run(go())


def test_degraded_unit_retries_with_backoff_then_comes_up(tmp_path):
    async def go():
        eng = _engine(tmp_path, retry_base=0.02, retry_cap=0.05)
        target = manifest(alpha=("1",)).with_instance("opt:down", "default", InstanceSpec("opt:down", "1", {"works_on": 4}))
        assert (await eng.apply(target)).degraded
        assert eng.retry_delay(1) == 0.02 and eng.retry_delay(2) == 0.04 and eng.retry_delay(5) == 0.05
        for _ in range(200):
            await asyncio.sleep(0.02)
            if "opt:down@default" in eng.manifest.instances:
                break
        assert "opt:down@default" in {u["ref"] for u in eng.status()["units"]}
        assert Unreachable.attempts == 4 and not eng.status()["degraded"] and not eng.status()["failed"]
        await eng.shutdown()
    run(go())


def test_core_unit_still_aborts_even_beside_an_optional_failure(tmp_path):
    async def go():
        eng = _engine(tmp_path, retry_base=3600.0)
        await eng.load("alpha", version="1")
        gen = eng.generation
        target = (eng.manifest.with_instance("beta", "default", InstanceSpec("beta", "1", {"health": "failed"}))
                  .with_instance("opt:down", "default", InstanceSpec("opt:down", "1", {})))
        report = await eng.apply(target)
        assert not report.ok and "beta@default" in report.failed
        assert eng.generation == gen and [u["ref"] for u in eng.status()["units"]] == ["alpha@default"]
        assert not eng.status()["degraded"]
        await eng.shutdown()
    run(go())
