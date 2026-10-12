"""A Hermes-external dispatch (dispatch_swarm_goal) as a task on the work board: an identical re-dispatch never calls Hermes
twice, the dispatcher holds a lease and heartbeats while Hermes works, results and failures land on the task, and a crash
leaves a task that expires and is retried or shows as failed. The Hermes round trip is a fake coroutine; the board is a
LocalBoard of the test's own."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances, coordination, hermes_config
from bot.backends.base import BackendResult
from bot.coordination import LocalBoard, hermes_board
from bot.dashboard.server import build_app
from coordination_fakes import FakeClock


def _args(goal="build the thing", provider="openrouter", model="free-model", n=3):
    return (7, goal, provider, model, n)


def _tasks(board):
    return board.list_tasks(hermes_board.board_name(7))


def test_a_dispatch_is_a_task_with_a_stable_key_and_its_result_is_recorded(tmp_path):
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)
    calls = []

    async def hermes():
        calls.append(1)
        return {"text": "all done", "job_id": 5, "children": [{"goal": "a"}]}

    out = asyncio.run(hermes_board.run(*_args(), hermes))
    assert out.result["text"] == "all done" and not out.deduped and calls == [1]
    [task] = _tasks(board)
    full = board.get_task(task["id"])
    assert full["state"] == "done" and full["result"]["children"] == [{"goal": "a"}]
    assert full["board"] == "abp/hermes-7" and full["key"] == hermes_board.dispatch_key(*_args())
    # goal, target and params all change the key
    keys = {hermes_board.dispatch_key(*_args(**kw)) for kw in ({}, {"goal": "other"}, {"provider": "x"}, {"model": "y"}, {"n": 4})}
    assert len(keys) == 5


def test_a_repeat_while_the_first_runs_waits_for_it_and_hermes_is_called_once(tmp_path):
    coordination.set_board(LocalBoard(tmp_path / "b.db"))
    calls = []

    async def scenario():
        gate = asyncio.Event()

        async def hermes():
            calls.append(1)
            await gate.wait()
            return {"text": "the answer"}

        first = asyncio.ensure_future(hermes_board.run(*_args(), hermes, poll_s=0.02))
        await asyncio.sleep(0.1)
        second = asyncio.ensure_future(hermes_board.run(*_args(), hermes, poll_s=0.02))
        await asyncio.sleep(0.1)
        assert not second.done()
        gate.set()
        return await first, await second

    a, b = asyncio.run(scenario())
    assert calls == [1]
    assert not a.deduped and b.deduped and b.result == {"text": "the answer"} and b.task == a.task


def test_a_finished_identical_dispatch_answers_a_repeat_but_a_different_goal_runs(tmp_path):
    coordination.set_board(LocalBoard(tmp_path / "b.db"))
    calls = []

    async def hermes():
        calls.append(1)
        return {"text": f"run {len(calls)}"}

    async def scenario():
        first = await hermes_board.run(*_args(), hermes)
        again = await hermes_board.run(*_args(), hermes)
        other = await hermes_board.run(*_args(goal="another goal"), hermes)
        return first, again, other

    first, again, other = asyncio.run(scenario())
    assert again.deduped and again.result == first.result
    assert not other.deduped and other.result == {"text": "run 2"} and len(calls) == 2


def test_an_old_finished_dispatch_is_not_reused(tmp_path, monkeypatch):
    coordination.set_board(LocalBoard(tmp_path / "b.db"))
    monkeypatch.setattr(hermes_board, "REUSE_DONE_S", -1.0)
    calls = []

    async def hermes():
        calls.append(1)
        return {"text": "x"}

    async def scenario():
        await hermes_board.run(*_args(), hermes)
        return await hermes_board.run(*_args(), hermes)

    out = asyncio.run(scenario())
    assert not out.deduped and len(calls) == 2


def test_the_dispatcher_keeps_its_lease_alive_while_hermes_is_slow(tmp_path):
    board = LocalBoard(tmp_path / "b.db", min_lease_s=0.05)
    coordination.set_board(board)
    seen = {}

    async def hermes():
        await asyncio.sleep(0.15)
        [t] = _tasks(board)
        seen["first"] = (t["state"], t["owner"], t["lease_until"])
        await asyncio.sleep(0.5)                               # several leases long
        [t] = _tasks(board)
        seen["later"] = (t["state"], t["lease_until"], t["attempts"])
        return {"text": "slow but fine"}

    out = asyncio.run(hermes_board.run(*_args(), hermes, lease_s=0.15))
    assert out.result == {"text": "slow but fine"}
    assert seen["first"][0] == "running" and seen["first"][1].startswith("hermes-dispatch:")
    assert seen["later"][0] == "running" and seen["later"][1] > seen["first"][2] and seen["later"][2] == 0   # renewed, never expired
    assert _tasks(board)[0]["state"] == "done"


def test_a_failed_dispatch_is_recorded_reraised_and_retryable(tmp_path):
    board = LocalBoard(tmp_path / "b.db")
    coordination.set_board(board)

    async def boom():
        raise RuntimeError("hermes gateway exploded")

    async def fine():
        return {"text": "second time lucky"}

    async def scenario():
        with pytest.raises(RuntimeError, match="exploded"):
            await hermes_board.run(*_args(), boom)
        return await hermes_board.run(*_args(), fine)

    out = asyncio.run(scenario())
    assert not out.deduped and out.result == {"text": "second time lucky"}
    first, second = sorted(_tasks(board), key=lambda t: t["created_at"])
    assert first["state"] == "failed" and "exploded" in first["note"]
    assert second["state"] == "done"


def test_a_crash_mid_run_expires_and_the_next_dispatch_retries_it(tmp_path):
    clock = FakeClock()
    board = LocalBoard(tmp_path / "b.db", clock=clock, min_lease_s=0.01)
    coordination.set_board(board)
    task, _ = board.create_task("abp/hermes-7", "hermes dispatch", key=hermes_board.dispatch_key(*_args()), max_attempts=2)
    board.claim(task["id"], "hermes-dispatch:dead", lease_s=1)       # ABP died holding it
    calls = []

    async def hermes():
        calls.append(1)
        return {"text": "retried"}

    async def scenario():
        waiting = asyncio.ensure_future(hermes_board.run(*_args(), hermes, poll_s=0.02))
        await asyncio.sleep(0.2)
        assert not waiting.done() and calls == []                    # the dead run's lease still stands
        clock.advance(5)
        return await asyncio.wait_for(waiting, 10)

    out = asyncio.run(scenario())
    assert out.result == {"text": "retried"} and calls == [1]
    final = board.get_task(task["id"])
    assert final["state"] == "done" and final["attempts"] == 1


def test_a_task_whose_runner_keeps_dying_ends_failed_and_a_repeat_is_a_new_task(tmp_path):
    clock = FakeClock()
    board = LocalBoard(tmp_path / "b.db", clock=clock, min_lease_s=0.01)
    coordination.set_board(board)
    task, _ = board.create_task("abp/hermes-7", "hermes dispatch", key=hermes_board.dispatch_key(*_args()), max_attempts=2)
    for i in range(2):
        board.claim(task["id"], f"hermes-dispatch:dead{i}", lease_s=1)
        clock.advance(5)
    final = board.get_task(task["id"])
    assert final["state"] == "failed" and "lease expired" in final["note"]

    async def hermes():
        return {"text": "fresh"}

    out = asyncio.run(hermes_board.run(*_args(), hermes))            # failed is terminal: the repeat is a new task
    assert not out.deduped and out.result == {"text": "fresh"}


def test_a_dispatch_runs_unboarded_when_the_board_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "enabled", lambda: False)

    async def hermes():
        return {"text": "no board"}

    out = asyncio.run(hermes_board.run(*_args(), hermes))
    assert out.result == {"text": "no board"} and out.task == ""


# --------------------------------------------------------- through the route
def test_the_dispatch_route_does_not_call_hermes_twice_for_the_same_goal(temp_db, monkeypatch, tmp_path):
    from bot.router import router

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    client = TestClient(build_app())
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")
    iid = bot_instances.create_instance(
        name="hermes-worker", platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[111], enabled=False)
    asked = []

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        asked.append(prompt)
        return BackendResult(text="all subtasks done", tokens=None, raw=None)

    monkeypatch.setattr(router, "ask", fake_ask)
    body = {"goal": "build the thing", "worker_provider": "ollama", "worker_model": "llama3:free"}
    h = {"X-Dashboard-Token": "test-token"}
    one = client.post(f"/api/hermes/{iid}/dispatch", headers=h, json=body)
    two = client.post(f"/api/hermes/{iid}/dispatch", headers=h, json=body)
    assert one.status_code == two.status_code == 200
    assert len(asked) == 1 and two.json()["result"] == "all subtasks done" and two.json()["deduped"] is True
    assert "deduped" not in one.json()
    [task] = coordination.get_board().list_tasks(f"abp/hermes-{iid}")
    assert task["state"] == "done"
