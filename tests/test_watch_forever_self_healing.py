"""bot/hotreload.py's watch_forever() and bot/config.py's
ConfigManager.watch_forever() — both wrap watchfiles.awatch() in an outer
restart loop now, so a real (documented) awatch() failure (a deleted
watched path, a permission change, an OS-level file-watching backend
hiccup) can't silently, permanently disable hot-reload / config-file
watching for the rest of the process's life. Same class of bug this
project's dashboard-port-bind crash already demonstrated once for a
different subsystem — these are the other two background watch loops
that had the identical structural gap (no try/except around the
`async for` itself), found by auditing every `*_forever()` loop bot/main.py
starts after fixing that one.
"""
from __future__ import annotations

import asyncio

from bot import hotreload


class _FailNTimesThenYield:
    """Stands in for watchfiles.awatch(): raises on the first `fail_times`
    calls (simulating awatch() itself dying), then yields one real change
    and blocks forever (so the test controls exactly when to stop it,
    the same way a real long-lived watch would keep running)."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise RuntimeError("simulated watcher crash")
        return self._good_generator()

    async def _good_generator(self):
        yield {(1, "bot/somefile.py")}
        await asyncio.sleep(10)  # block "forever" — the test cancels the task


def test_hotreload_watch_forever_restarts_after_awatch_crashes(monkeypatch):
    import watchfiles

    fake_awatch = _FailNTimesThenYield(fail_times=2)
    monkeypatch.setattr(watchfiles, "awatch", fake_awatch)

    cycles = []

    async def fake_run_cycle(paths, **kwargs):
        cycles.append(paths)

    monkeypatch.setattr(hotreload, "run_cycle", fake_run_cycle)
    # The real 2s backoff between restarts would make this test slow for
    # no reason — the behavior under test is "did it restart," not "how
    # long did it wait." hotreload.asyncio IS the real asyncio module (a
    # plain `import asyncio`), so the replacement must close over the
    # ORIGINAL sleep, not call through the name "asyncio.sleep" again —
    # that would recurse into itself once patched.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(hotreload.asyncio, "sleep", lambda _s: _real_sleep(0))

    async def _run():
        task = asyncio.create_task(hotreload.watch_forever())
        for _ in range(200):
            if cycles:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert fake_awatch.call_count == 3  # 2 crashes + 1 that actually ran
    assert len(cycles) == 1


def test_hotreload_watch_forever_survives_a_bad_reload_cycle(monkeypatch):
    """A single run_cycle() failure must only skip that one cycle, never
    take the whole watcher down with it — distinct from (and cheaper
    than) needing the outer awatch()-crashed restart path at all."""
    import watchfiles

    async def one_shot_then_block(*_a, **_kw):
        yield {(1, "bot/somefile.py")}
        await asyncio.sleep(10)

    monkeypatch.setattr(watchfiles, "awatch", one_shot_then_block)

    async def failing_run_cycle(paths, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hotreload, "run_cycle", failing_run_cycle)

    async def _run():
        task = asyncio.create_task(hotreload.watch_forever())
        await asyncio.sleep(0.05)
        still_alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return still_alive

    still_alive = asyncio.run(_run())

    assert still_alive  # the exception inside run_cycle did not kill the task
