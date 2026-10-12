"""The work board wired into ABP: sub-agent children, edit locks, swarm steps. Real run_batch / execute_tool / strategies
against scripted backends; the board is a LocalBoard (the conftest points it at a database of the test's own)."""
from __future__ import annotations

import asyncio
import collections

import pytest

from bot import bot_instances, coordination
from bot.agent_runtime import subagent_registry, subagents
from bot.agent_runtime import tools as agent_tools
from bot.agent_runtime.errors import ToolError
from bot.backends.base import BackendResult
from bot.config import config
from bot.coordination import LocalBoard, WorkSession, subagent_board
from bot.swarm import strategies
from coordination_fakes import FakeClock


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    monkeypatch.setattr(subagent_registry, "_dispatches", collections.OrderedDict())


def _instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[111], enabled=False)


class Scripted:
    """A child backend: `ask` runs `script(prompt)` (a coroutine function) and counts its calls."""

    def __init__(self, script):
        self.model, self.name, self.script, self.calls = "fake-model", "native_agent", script, []

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.calls.append(prompt)
        return BackendResult(text=await self.script(prompt), tokens=None, raw=None)


def _use(monkeypatch, backend):
    monkeypatch.setattr(subagents, "_resolve_inherited_backend", lambda parent_instance_id: backend)


# ---------------------------------------------------- edit locks across children
def test_two_subagents_cannot_edit_the_same_file(temp_db, monkeypatch, tmp_path):
    instance = _instance()
    ws = tmp_path / "ws"
    ws.mkdir()
    state: dict = {}

    async def script(prompt):
        if "first" not in state:
            state["first"] = True
            state["gate"] = asyncio.Event()
            await agent_tools.execute_tool("write_file", {"path": "shared.txt", "content": "from A"}, workspace=ws)
            await asyncio.sleep(0.05)
            state["a_wrote"] = True
            await state["gate"].wait()                  # A is still working (and still holds the file) while B tries
            return "A done"
        await asyncio.sleep(0.02)
        try:
            await agent_tools.execute_tool("write_file", {"path": "shared.txt", "content": "from B"}, workspace=ws)
            out = "B wrote"
        except ToolError as exc:
            out = f"B refused: {exc}"
        state["gate"].set()
        return out

    _use(monkeypatch, Scripted(script))
    result = asyncio.run(subagents.run_batch([{"goal": "write the notes (task A)"}, {"goal": "also write the notes (task B)"}],
                                             parent_instance_id=instance, workspace=ws))
    texts = [c["result_excerpt"] for c in result["children"]]
    assert texts[0] == "A done"
    assert texts[1].startswith("B refused") and "shared.txt" in texts[1]
    assert "sub:" in texts[1] and "write the notes (task A)" in texts[1]          # names the holder: agent and task title
    assert (ws / "shared.txt").read_text() == "from A"


def test_after_the_holder_finishes_the_file_is_free(temp_db, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    async def scenario():
        board = coordination.get_board()
        a, _ = board.create_task("t", "task A")
        b, _ = board.create_task("t", "task B")
        async with await WorkSession.claim(board, a["id"], "agent-a", lease_s=30):
            await agent_tools.execute_tool("write_file", {"path": "f.txt", "content": "A"}, workspace=ws)
            c, _ = board.create_task("t", "task C")
            async with await WorkSession.claim(board, c["id"], "agent-c", lease_s=30):
                with pytest.raises(ToolError, match="agent-a"):
                    await agent_tools.execute_tool("write_file", {"path": "f.txt", "content": "C"}, workspace=ws)
                await agent_tools.execute_tool("write_file", {"path": "other.txt", "content": "C"}, workspace=ws)   # other files are fine
        async with await WorkSession.claim(board, b["id"], "agent-b", lease_s=30):      # A is done: the file is free
            await agent_tools.execute_tool("write_file", {"path": "f.txt", "content": "B"}, workspace=ws)

    asyncio.run(scenario())
    assert (ws / "f.txt").read_text() == "B"


def test_a_patch_is_refused_when_one_of_its_files_is_held(temp_db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one\n")
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"

    async def scenario():
        board = coordination.get_board()
        t, _ = board.create_task("t", "holder task")
        async with await WorkSession.claim(board, t["id"], "holder", lease_s=30) as w:
            from bot.coordination import fileguard
            await w.acquire([fileguard.resource_for(ws, "a.txt")])
            u, _ = board.create_task("t", "other")
            async with await WorkSession.claim(board, u["id"], "other", lease_s=30):
                with pytest.raises(ToolError, match="holder"):
                    await agent_tools.execute_tool("apply_patch", {"patch": patch}, workspace=ws)

    asyncio.run(scenario())
    assert (ws / "a.txt").read_text() == "one\n"


def test_a_lone_agent_takes_no_locks(temp_db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    board = coordination.get_board()

    async def scenario():
        await agent_tools.execute_tool("write_file", {"path": "solo.txt", "content": "x"}, workspace=ws)

    asyncio.run(scenario())
    assert (ws / "solo.txt").read_text() == "x" and board.list_locks() == []


# ------------------------------------------------------------ duplicate spawn
def test_a_duplicate_spawn_returns_the_existing_result(temp_db, monkeypatch):
    instance = _instance()

    async def script(prompt):
        await asyncio.sleep(0.02)
        return f"result for {prompt}"

    backend = Scripted(script)
    _use(monkeypatch, backend)

    async def scenario():
        subagent_board.turn_var.set("turn-1")
        one = await subagents.run_batch([{"goal": "summarise the repo"}], parent_instance_id=instance)
        again = await subagents.run_batch([{"goal": "summarise the repo"}], parent_instance_id=instance)   # a retried tool call
        other_turn = None
        subagent_board.turn_var.set("turn-2")
        other_turn = await subagents.run_batch([{"goal": "summarise the repo"}], parent_instance_id=instance)
        return one, again, other_turn

    one, again, other_turn = asyncio.run(scenario())
    assert len(backend.calls) == 2                                  # the retry did not run; a new turn legitimately does
    assert again["children"][0]["result_excerpt"] == one["children"][0]["result_excerpt"]
    assert again["children"][0].get("deduped") is True
    assert other_turn["children"][0].get("deduped") is None


def test_identical_children_in_one_batch_run_once(temp_db, monkeypatch):
    instance = _instance()

    async def script(prompt):
        await asyncio.sleep(0.05)
        return "the answer"

    backend = Scripted(script)
    _use(monkeypatch, backend)
    result = asyncio.run(subagents.run_batch([{"goal": "same"}, {"goal": "same"}, {"goal": "different"}], parent_instance_id=instance))
    assert len(backend.calls) == 2
    assert [c["status"] for c in result["children"]] == ["ok", "ok", "ok"]
    assert result["children"][0]["result_excerpt"] == result["children"][1]["result_excerpt"] == "the answer"


def test_list_subagents_shows_the_board_state(temp_db, monkeypatch):
    instance = _instance()

    async def script(prompt):
        return "fine"

    _use(monkeypatch, Scripted(script))

    async def scenario():
        out = await subagents.run_batch([{"goal": "look around"}], parent_instance_id=instance)
        return subagent_registry.describe(out["dispatch_id"], parent_instance_id=instance)

    desc = asyncio.run(scenario())
    assert desc["children"][0]["board"]["state"] == "done"
    assert desc["children"][0]["board"]["board"].startswith("abp/")


def test_a_child_that_fails_is_a_failed_task(temp_db, monkeypatch):
    instance = _instance()

    async def script(prompt):
        raise RuntimeError("model fell over")

    _use(monkeypatch, Scripted(script))
    out = asyncio.run(subagents.run_batch([{"goal": "doomed"}], parent_instance_id=instance))
    assert out["children"][0]["status"] == "error"
    states = [t["state"] for t in coordination.get_board().list_tasks()]
    assert states == ["failed"]


# ------------------------------------------------------- a crashed child
def test_a_crashed_childs_lease_expires_and_its_task_is_retried(temp_db, monkeypatch, tmp_path):
    clock = FakeClock()
    board = LocalBoard(tmp_path / "crash.db", clock=clock, min_lease_s=0.01)
    coordination.set_board(board)
    instance = _instance()
    backend = Scripted(lambda p: asyncio.sleep(0, result="finished by the retry"))
    _use(monkeypatch, backend)
    try:
        # A child of the same spawn started, took the task, and died without a word.
        subagent_board.turn_var.set("turn-x")
        key = subagent_board.child_key("turn-x", "", "do the thing")
        name = subagent_board.board_name_for(instance)
        task, _ = board.create_task(name, "subagent: do the thing", key=key)
        board.claim(task["id"], "sub:dead:0", lease_s=1)

        async def scenario():
            # The parent retries the spawn while the dead child's lease is still running: it finds the task and waits.
            running = asyncio.ensure_future(subagents.run_batch([{"goal": "do the thing"}], parent_instance_id=instance))
            await asyncio.sleep(0.5)
            assert not running.done() and backend.calls == []        # not run twice, not run yet
            clock.advance(5)                                         # the lease runs out
            return await asyncio.wait_for(running, 10)

        out = asyncio.run(scenario())
        assert out["children"][0]["result_excerpt"] == "finished by the retry" and len(backend.calls) == 1
        final = board.get_task(task["id"])
        assert final["state"] == "done" and final["attempts"] == 1
    finally:
        subagent_board.turn_var.set(None)
        coordination.set_board(None)
        board.close()


# ------------------------------------------------------------------ swarms
@pytest.fixture
def members(monkeypatch):
    calls: list = []
    running = {"n": 0, "max": 0}
    script: dict = {}

    async def fake_run_member(instance_id, prompt, *, swarm_run_id, action_type="quick_question", user_id=0):
        calls.append((instance_id, prompt))
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        try:
            await asyncio.sleep(0.03)
            behaviour = script.get(instance_id)
            if isinstance(behaviour, BaseException):
                raise behaviour
            return behaviour(prompt) if behaviour else f"out{instance_id}"
        finally:
            running["n"] -= 1

    monkeypatch.setattr(strategies, "run_member", fake_run_member)
    monkeypatch.setattr(strategies, "instance_label", lambda i: f"bot{i}")
    return calls, running, script


def run_swarm(strategy, config_, prompt="Q?", run_id="r1", swarm_id=7):
    return strategies.STRATEGIES[strategy]().run({"id": swarm_id, "config": config_}, prompt, swarm_run_id=run_id)


def test_sequential_relay_runs_strictly_in_order_through_board_deps(temp_db, members):
    calls, running, _ = members

    async def scenario():
        result = await run_swarm("sequential_relay", {"members": [1, 2, 3]})
        return result, coordination.get_board().list_tasks("abp/swarm-7")

    result, tasks = asyncio.run(scenario())
    assert [c[0] for c in calls] == [1, 2, 3] and running["max"] == 1       # a worker never ran a step before its dependency
    assert result.status == "success" and result.result == "out3"
    assert [t["state"] for t in tasks] == ["done"] * 3
    assert tasks[1]["deps"] == [tasks[0]["id"]] and tasks[2]["deps"] == [tasks[1]["id"]]


def test_a_failed_relay_link_leaves_nothing_dangling(temp_db, members):
    calls, _, script = members
    script[2] = RuntimeError("down")

    async def scenario():
        result = await run_swarm("sequential_relay", {"members": [1, 2, 3]})
        return result, coordination.get_board().list_tasks("abp/swarm-7")

    result, tasks = asyncio.run(scenario())
    assert result.status == "failed" and len(result.steps) == 2 and [c[0] for c in calls] == [1, 2]
    assert sorted(t["state"] for t in tasks) == ["cancelled", "done", "failed"]


def test_fanout_workers_pull_all_steps_in_parallel(temp_db, members):
    calls, running, _ = members
    result = asyncio.run(run_swarm("fanout_synthesize", {"members": [1, 2, 3, 4], "synthesizer": 9}))
    assert result.status == "success" and result.result == "out9" and running["max"] >= 3
    assert len(calls) == 5


def test_redispatching_the_same_swarm_run_does_not_repeat_steps(temp_db, members):
    calls, _, _ = members

    async def scenario():
        return await asyncio.gather(run_swarm("sequential_relay", {"members": [1, 2, 3]}, run_id="rA"),
                                    run_swarm("sequential_relay", {"members": [1, 2, 3]}, run_id="rB"))

    a, b = asyncio.run(scenario())
    assert len(calls) == 3                                           # three steps, each done once, by whichever run got there
    assert a.result == b.result == "out3" and a.status == b.status == "success"


def test_custom_graph_orders_by_dependencies(temp_db, members):
    calls, _, script = members
    script[3] = RuntimeError("one input fails")
    steps = [{"id": "a", "instance_id": 1}, {"id": "b", "instance_id": 3},
             {"id": "c", "instance_id": 2, "depends_on": ["a", "b"]}]
    result = asyncio.run(run_swarm("custom", {"steps": steps}))
    assert result.status == "partial"                                # a failed input still unblocks its dependent
    order = [c[0] for c in calls]
    assert order.index(2) > order.index(1) and order.index(2) > order.index(3)
    assert "— a —" in calls[-1][1] and "— b —" not in calls[-1][1]


def test_decompose_plans_then_workers_then_aggregator(temp_db, members):
    calls, _, script = members
    script[1] = lambda p: '["x", "y"]'
    result = asyncio.run(run_swarm("decompose_delegate", {"planner": 1, "members": [2, 3], "aggregator": 4}))
    assert result.status == "success" and result.result == "out4"
    assert [c[0] for c in calls][0] == 1 and [c[0] for c in calls][-1] == 4
    board = coordination.get_board()
    titles = [t["title"] for t in board.list_tasks("abp/swarm-7")]
    assert any(t.startswith("planner") for t in titles) and any(t.startswith("aggregator") for t in titles)


# ------------------------------------------------------------------ the routes
def test_the_summary_and_task_routes(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    h = {"X-Dashboard-Token": "test-token"}
    board = coordination.get_board()
    board.create_task("abp/demo", "something to do")
    client = TestClient(build_app())
    assert client.get("/api/coordination/summary").status_code == 401
    s = client.get("/api/coordination/summary", headers=h).json()
    assert s["backend"] == "local" and s["summary"]["boards"]["abp/demo"]["counts"]["open"] == 1
    t = client.get("/api/coordination/tasks", headers=h, params={"board": "abp/demo"}).json()
    assert [x["title"] for x in t["tasks"]] == ["something to do"]
    assert client.get("/api/coordination/locks", headers=h).json()["locks"] == []
