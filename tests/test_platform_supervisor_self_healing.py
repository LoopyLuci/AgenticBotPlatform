"""bot/platform_supervisor.py — a bot instance whose task ends with an
unhandled exception used to just sit dead until a human noticed and
clicked restart in the dashboard. It now restarts itself automatically
(with backoff), the same "recover on its own" treatment every other
background loop in this app already gets. A *deliberate* stop (cancel)
must still leave the instance stopped, never trigger a restart.
"""
from __future__ import annotations

import asyncio

from bot import bot_instances, platform_supervisor


def _run(coro):
    return asyncio.run(coro)


def _create_instance(temp_db, monkeypatch):
    # Bypass the format validator instead of supplying any token-shaped
    # string — a plausible-looking value here (even a fake one) can trip
    # GitHub's push protection secret scanner, and the actual credential
    # content is irrelevant to this test either way (_RUNNERS is faked
    # below, so nothing ever connects to a real Discord API).
    monkeypatch.setitem(bot_instances.PLATFORM_TOKEN_VALIDATORS["discord"], "bot_token", lambda v: (True, "ok"))
    return bot_instances.create_instance(
        name="test-bot", platform="discord", backend="cli",
        credentials={"bot_token": "unused"},
        allowed_user_ids=[1],
    )


def test_a_crashed_instance_restarts_itself(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db, monkeypatch)
    row = bot_instances.get_instance(instance_id)
    monkeypatch.setattr(platform_supervisor, "_restart_state", {})

    run_count = {"n": 0}

    async def flaky_runner(row):
        run_count["n"] += 1
        if run_count["n"] == 1:
            raise RuntimeError("boom")
        await asyncio.Event().wait()  # second run: behaves, blocks until cancelled

    monkeypatch.setitem(platform_supervisor._RUNNERS, "discord", flaky_runner)

    _real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(platform_supervisor.asyncio, "sleep", fast_sleep)

    async def _scenario():
        await platform_supervisor.start_instance(row)
        for _ in range(200):
            if run_count["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert run_count["n"] == 2
        assert platform_supervisor.is_running(instance_id)
        await platform_supervisor.stop_instance(instance_id)

    _run(_scenario())


def test_a_deliberately_stopped_instance_does_not_restart(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db, monkeypatch)
    row = bot_instances.get_instance(instance_id)
    monkeypatch.setattr(platform_supervisor, "_restart_state", {})

    run_count = {"n": 0}

    async def blocking_runner(row):
        run_count["n"] += 1
        await asyncio.Event().wait()

    monkeypatch.setitem(platform_supervisor._RUNNERS, "discord", blocking_runner)

    async def _scenario():
        await platform_supervisor.start_instance(row)
        await asyncio.sleep(0.05)
        await platform_supervisor.stop_instance(instance_id)
        await asyncio.sleep(0.2)  # give a wrongly-triggered restart a chance to happen
        assert run_count["n"] == 1
        assert not platform_supervisor.is_running(instance_id)

    _run(_scenario())


def test_a_disabled_instance_is_not_restarted_after_crashing(temp_db, monkeypatch):
    instance_id = _create_instance(temp_db, monkeypatch)
    row = bot_instances.get_instance(instance_id)
    monkeypatch.setattr(platform_supervisor, "_restart_state", {})

    run_count = {"n": 0}

    async def crashing_runner(row):
        run_count["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setitem(platform_supervisor._RUNNERS, "discord", crashing_runner)

    _real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(platform_supervisor.asyncio, "sleep", fast_sleep)
    bot_instances.update_instance(instance_id, enabled=False)

    async def _scenario():
        await platform_supervisor.start_instance(row)
        for _ in range(50):
            if run_count["n"] >= 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert run_count["n"] == 1  # the crash happened once, but no restart followed

    _run(_scenario())
