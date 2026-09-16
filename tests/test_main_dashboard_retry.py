"""bot/main.py's _start_dashboard() — retries a transient dashboard-port
bind failure instead of letting it take the whole process down.

Real-world context: uvicorn's own Server.startup() calls
`sys.exit(uvicorn.config.STARTUP_FAILURE)` (== 3) directly on an OSError
from binding the socket — not a normal, catchable Exception. Confirmed
live (reproduced against a real held port) that a Task whose coroutine
raises SystemExit is NOT handled like a Task raising a normal exception:
asyncio's own Task-stepping machinery lets SystemExit/KeyboardInterrupt
propagate straight out of the event loop instead of storing them for a
later `.result()` call — the first version of this fix looked correct
but still let the whole process die on the very first bind failure,
because it only caught the exception after the fact via `.result()`,
never inside the coroutine frame uvicorn actually raises it from. These
tests exist specifically so that regression can never silently return.
"""
from __future__ import annotations

import asyncio

from bot import main as bot_main


def _make_fake_server_class(fail_times: int):
    """Builds a fresh class standing in for uvicorn.Server: raises
    SystemExit(3) — uvicorn's real bind-failure signal, not a normal
    Exception — for `fail_times` attempts, then behaves like a normal
    running server (sets `started`, blocks until `should_exit`). A fresh
    class per call keeps `attempts`/`failures_remaining` state isolated
    between tests, since _start_dashboard() constructs a new instance —
    but of this SAME class — on every retry within one test."""
    attempts: list[int] = []

    class FakeServer:
        failures_remaining = fail_times

        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        async def serve(self):
            attempts.append(1)
            if type(self).failures_remaining > 0:
                type(self).failures_remaining -= 1
                raise SystemExit(3)
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.01)

    return FakeServer, attempts


def _patch_uvicorn(monkeypatch, fake_server_class):
    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", fake_server_class)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: object())


def test_recovers_after_a_transient_bind_failure(monkeypatch):
    FakeServer, attempts = _make_fake_server_class(fail_times=2)
    _patch_uvicorn(monkeypatch, FakeServer)

    async def _run():
        server, task = await bot_main._start_dashboard(
            dash_app=None, host="127.0.0.1", port=1234, max_attempts=5, retry_delay_s=0.01,
        )
        try:
            assert server is not None
            assert server.started is True
            assert len(attempts) == 3  # 2 failures + 1 success
        finally:
            server.should_exit = True
            await task

    asyncio.run(_run())


def test_gives_up_gracefully_after_exhausting_every_attempt(monkeypatch, caplog):
    FakeServer, attempts = _make_fake_server_class(fail_times=99)  # never succeeds
    _patch_uvicorn(monkeypatch, FakeServer)

    async def _run():
        return await bot_main._start_dashboard(
            dash_app=None, host="127.0.0.1", port=1234, max_attempts=3, retry_delay_s=0.01,
        )

    with caplog.at_level("CRITICAL", logger="bot.main"):
        server, task = asyncio.run(_run())

    assert server is None
    assert task is None
    assert len(attempts) == 3
    assert any("could not bind" in r.message for r in caplog.records)


def test_a_bind_failure_never_escapes_as_an_uncaught_systemexit(monkeypatch):
    """The exact bug this whole module exists to prevent: SystemExit from
    a failed bind attempt must never propagate out of _start_dashboard()
    itself — asyncio.run() below would otherwise let it kill this test
    process's own event loop the same way it killed the real one."""
    FakeServer, _attempts = _make_fake_server_class(fail_times=1)
    _patch_uvicorn(monkeypatch, FakeServer)

    async def _run():
        server, task = await bot_main._start_dashboard(
            dash_app=None, host="127.0.0.1", port=1234, max_attempts=5, retry_delay_s=0.01,
        )
        server.should_exit = True
        await task

    asyncio.run(_run())  # must not raise SystemExit
