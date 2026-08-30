"""Router._invalidate() (fired on every config hot-reload, config/on_reload
in bot/config.py) used to just drop the cached backend dict with no
shutdown call, silently leaking anything holding a live external
process/connection - HermesGatewayBackend's spawned `hermes serve`, in
practice, on every backends.yaml edit. Confirms the fix: the old backend
set gets its shutdown() awaited (via a scheduled task, since _invalidate
itself is a sync callback), and a fresh backend is buildable immediately
without waiting on that teardown.
"""

from __future__ import annotations

import asyncio

from bot.router import Router


class _FakeBackend:
    def __init__(self):
        self.shutdown_called = False

    async def shutdown(self):
        self.shutdown_called = True


def test_invalidate_shuts_down_old_backends():
    async def _run():
        router = Router()
        fake = _FakeBackend()
        router._backends = {"api": fake}

        router._invalidate()
        assert router._backends == {}  # cache cleared immediately
        assert not fake.shutdown_called  # shutdown is scheduled, not synchronous

        await asyncio.sleep(0)  # let the scheduled task run
        assert fake.shutdown_called

    asyncio.run(_run())


def test_invalidate_with_no_backends_is_a_no_op():
    async def _run():
        router = Router()
        router._invalidate()  # must not raise with nothing to shut down
        assert router._backends == {}

    asyncio.run(_run())


def test_invalidate_outside_an_event_loop_does_not_raise():
    router = Router()
    router._backends = {"api": _FakeBackend()}
    router._invalidate()  # no running loop here — must degrade to a warning, not crash
    assert router._backends == {}


def test_shutdown_backends_awaits_every_backend_directly():
    async def _run():
        router = Router()
        fake = _FakeBackend()
        router._backends = {"api": fake}
        await router.shutdown_backends()
        assert fake.shutdown_called

    asyncio.run(_run())


def test_invalidate_does_not_crash_on_a_failing_shutdown():
    class _BrokenBackend:
        async def shutdown(self):
            raise RuntimeError("boom")

    async def _run():
        router = Router()
        router._backends = {"api": _BrokenBackend()}
        router._invalidate()
        await asyncio.sleep(0)  # the scheduled task's exception must be caught, not surfaced

    asyncio.run(_run())
