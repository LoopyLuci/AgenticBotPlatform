"""The agent loop's limits, circle detection, parallel read-only calls, and what happens
to the conversation when a turn is cancelled part-way (roadmap P1)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say
from bot import db
from bot.agent_runtime import loop_guard, toolspec, tools
from bot.agent_runtime.loop_guard import Limits, Watchdog
from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.agent_runtime.transports.base import NormalizedResponse, ToolCall
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
from bot.backends.native_backend import NativeAgentBackend


def tc(name, **args):
    return ToolCall(id=f"id-{name}-{len(args)}", name=name, arguments=args)


# ---- limits ---------------------------------------------------------------------
def test_limits_come_from_config_with_sane_defaults(monkeypatch):
    from bot import config as cfg

    monkeypatch.setattr(cfg.config, "_data", {"native_agent": {"limits": {"max_iterations": 7, "max_seconds": "12"}}})
    lim = loop_guard.limits()
    assert (lim.max_iterations, lim.max_seconds, lim.max_tokens) == (7, 12.0, 0)


def test_bad_limit_values_fall_back_to_defaults(monkeypatch):
    from bot import config as cfg

    monkeypatch.setattr(cfg.config, "_data", {"native_agent": {"limits": {"max_iterations": "lots"}}})
    assert loop_guard.limits().max_iterations == loop_guard.DEFAULT_MAX_ITERATIONS


def test_iteration_time_and_token_limits_stop_the_turn():
    w = Watchdog(Limits(max_iterations=2))
    assert w.before_call(0) is None and w.before_call(0) is None
    assert "2 steps" in w.before_call(0)
    assert "2 steps" in w.before_call(0)        # and it stays stopped

    now = [0.0]
    w = Watchdog(Limits(max_iterations=0, max_seconds=10), clock=lambda: now[0])
    assert w.before_call(0) is None
    now[0] = 11
    assert "10 seconds" in w.before_call(0)

    w = Watchdog(Limits(max_iterations=0, max_tokens=1000))
    assert w.before_call(999) is None and "1000 tokens" in w.before_call(1000)


def test_zero_turns_a_limit_off():
    w = Watchdog(Limits(max_iterations=0))
    assert all(w.before_call(10**9) is None for _ in range(500))


# ---- circles -------------------------------------------------------------------
def test_the_same_call_with_the_same_result_is_flagged_then_stops_the_turn():
    w = Watchdog(Limits())
    call = tc("read_file", path="a")
    notes = []
    for _ in range(5):
        notes = w.after_round([(call, "same output")])
    assert w.stop_reason and "read_file" in w.stop_reason
    w2 = Watchdog(Limits())
    seen = [w2.after_round([(call, "same output")])[0] for _ in range(3)]
    assert seen[0] == "" and seen[1] == "" and "3th time" in seen[2] and w2.stop_reason is None
    for _ in range(2):
        w2.after_round([(call, "same output")])
    assert "read_file" in w2.stop_reason


def test_changing_output_is_progress_not_a_circle():
    w = Watchdog(Limits())
    call = tc("shell_output", id="job1")
    for i in range(12):
        assert w.after_round([(call, f"line {i}")]) == [""]
    assert w.stop_reason is None


def test_rounds_where_everything_fails_are_flagged_then_stop_the_turn():
    w = Watchdog(Limits())
    notes = []
    for i in range(3):
        notes = w.after_round([(tc("read_file", path=str(i)), "Error: nope")])
    assert "rounds of tool calls all failed" in notes[-1]
    for i in range(3, 6):
        w.after_round([(tc("read_file", path=str(i)), "Error: nope")])
    assert w.stop_reason == "every tool call kept failing"
    ok = Watchdog(Limits())
    for i in range(10):
        ok.after_round([(tc("a", n=i), "Error: x"), (tc("b", n=i), "fine")])
    assert ok.stop_reason is None


# ---- parallel execution ----------------------------------------------------------
def test_consecutive_read_only_calls_run_together_and_keep_their_order():
    async def scenario():
        started = []

        async def run_one(call):
            started.append(call.name)
            await asyncio.sleep(0.3)
            return f"out-{call.name}"

        calls = [tc("read_file", n=1), tc("grep", n=2), tc("glob", n=3)]
        results: list = []
        t0 = time.monotonic()
        await loop_guard.run_calls(calls, run_one, toolspec.is_concurrency_safe, results)
        assert time.monotonic() - t0 < 0.7, "three 0.3s calls should overlap"
        assert [o for _, o in results] == ["out-read_file", "out-grep", "out-glob"]
    asyncio.run(scenario())


def test_a_write_between_reads_is_a_barrier():
    async def scenario():
        log = []

        async def run_one(call):
            log.append(("start", call.name))
            await asyncio.sleep(0.1)
            log.append(("end", call.name))
            return call.name

        calls = [tc("read_file", n=1), tc("grep", n=2), tc("write_file", n=3), tc("read_file", n=4), tc("glob", n=5)]
        results: list = []
        await loop_guard.run_calls(calls, run_one, toolspec.is_concurrency_safe, results)
        assert [o for _, o in results] == ["read_file", "grep", "write_file", "read_file", "glob"]
        starts = [i for i, e in enumerate(log) if e[0] == "start"]
        ends = [i for i, e in enumerate(log) if e[0] == "end"]
        assert ends[1] < starts[2] and ends[2] < starts[3], "the write must finish before the next read starts"
        assert starts[1] < ends[0], "the first two reads overlap"
    asyncio.run(scenario())


def test_parallelism_is_bounded():
    async def scenario():
        live, peak = 0, 0

        async def run_one(call):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return "x"

        calls = [tc("grep", n=i) for i in range(12)]
        await loop_guard.run_calls(calls, run_one, toolspec.is_concurrency_safe, [], max_parallel=3)
        assert peak == 3
    asyncio.run(scenario())


# ---- the loop ------------------------------------------------------------------
class Recording(ScriptedTransport):
    def __init__(self, script):
        super().__init__(script)
        self.histories = []

    async def send(self, **kw):
        self.histories.append(list(kw["history"]))
        return await super().send(**kw)


def ask(backend, prompt="go", **ctx):
    ctx.setdefault("cwd", str(ctx.pop("_cwd")))
    return asyncio.run(backend.ask(prompt, context=ctx))


def test_reaching_the_step_limit_returns_a_summary_not_an_error(temp_db, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(loop_guard, "limits", lambda: Limits(max_iterations=3))
    script = [Call("list_dir", {"path": str(i)}) for i in range(3)] + [Say("I listed some folders; nothing else done.")]
    t = Recording(script)
    result = ask(NativeAgentBackend(t, model="m"), _cwd=tmp_path)
    assert result.raw["stopped"].startswith("it reached the limit of 3 steps")
    assert "listed some folders" in result.text and "Say \"continue\"" in result.text
    assert t.calls == 4                                     # three real calls + the wrap-up
    assert "Do not call tools" in str(t.histories[-1][-1])


def test_a_turn_that_repeats_itself_is_stopped(temp_db, tmp_path):
    (tmp_path / "a.txt").write_text("same")
    script = [Call("read_file", {"path": "a.txt"}) for _ in range(12)] + [Say("I kept reading the same file.")]
    t = Recording(script)
    result = ask(NativeAgentBackend(t, model="m"), _cwd=tmp_path)
    assert "repeating the same read_file" in result.raw["stopped"]
    assert t.calls <= 7


def test_a_wrap_up_failure_still_returns_something(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(loop_guard, "limits", lambda: Limits(max_iterations=1))

    class Failing(ScriptedTransport):
        async def send(self, **kw):
            if kw["tool_schemas"] == []:
                raise RuntimeError("provider down")
            return await super().send(**kw)

    result = ask(NativeAgentBackend(Failing([Call("list_dir", {"path": "."})]), model="m"), _cwd=tmp_path)
    assert "Stopped" in result.text and result.raw["stopped"]


def test_parallel_reads_in_the_loop_answer_every_call(temp_db, tmp_path):
    (tmp_path / "a.txt").write_text("north")
    (tmp_path / "b.txt").write_text("south")
    t = Recording([Call("read_file", {"path": "a.txt"}, more=(("read_file", {"path": "b.txt"}),)), Say("north-south")])
    result = ask(NativeAgentBackend(t, model="m"), _cwd=tmp_path)
    assert result.text == "north-south"
    tool_results = [e["content"] for e in t.histories[-1] if str(e["content"]).startswith("tool result")]
    assert tool_results == ["tool result: north", "tool result: south"]


# ---- cancellation keeps the conversation valid ----------------------------------------
@pytest.fixture
def slow_tool():
    async def handler(inp, **kw):
        await asyncio.sleep(30)
        return "never"

    toolspec.register({"name": "slow_read", "description": "slow", "input_schema": {"type": "object", "properties": {}}},
                      toolspec.ToolSpec("slow_read", "read", read_only=True, concurrency_safe=True, origin="registered"),
                      handler)
    yield
    toolspec.unregister("slow_read")


def test_cancelling_mid_tool_answers_the_open_calls_so_the_session_stays_usable(temp_db, tmp_path, slow_tool):
    session = "native-cancel-test"

    async def scenario():
        t = Recording([Call("slow_read", {}, more=(("slow_read", {"n": 2}),))])
        backend = NativeAgentBackend(t, model="m")
        task = asyncio.ensure_future(backend.ask("go", context={"cwd": str(tmp_path), "desktop_session_key": session}))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    stored = db.list_agent_messages(session)
    # the scripted transport stores one result message per call: both open calls were answered
    assert [str(m["content"]).count("Cancelled") for m in stored[-2:]] == [1, 1]

    t2 = Recording([Say("back again")])
    result = ask(NativeAgentBackend(t2, model="m"), _cwd=tmp_path, desktop_session_key=session)
    assert result.text == "back again"


def test_a_session_left_dangling_by_an_older_crash_is_repaired_on_the_next_turn(temp_db, tmp_path):
    session = "native-dangling-test"
    db.append_agent_message(session, "user", "do it")
    db.append_agent_message(session, "assistant", [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}])

    class AnthropicShaped(AnthropicTransport):
        supports_vision = False
        supports_documents = False

        def __init__(self):
            super().__init__(api_key="unused")
            self.seen = None

        async def send(self, **kw):
            self.seen = list(kw["history"])
            return NormalizedResponse(text="ok", assistant_message={"role": "assistant", "content": "ok"})

    t = AnthropicShaped()
    asyncio.run(NativeAgentBackend(t, model="m").ask(
        "continue", context={"cwd": str(tmp_path), "desktop_session_key": session}))
    roles = [(e["role"], e["content"] if isinstance(e["content"], str) else e["content"][0].get("type")) for e in t.seen]
    assert roles == [("user", "do it"), ("assistant", "tool_use"), ("user", "tool_result"), ("user", "continue")]


def test_dangling_detection_per_transport():
    a = AnthropicTransport(api_key="unused")
    assert [c.id for c in a.dangling_tool_calls([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": [{"type": "text", "text": "hm"}, {"type": "tool_use", "id": "t1", "name": "a", "input": {}}]},
    ])] == ["t1"]
    assert a.dangling_tool_calls([{"role": "assistant", "content": "plain"}]) == []
    assert a.dangling_tool_calls([{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "a", "input": {}}]},
                                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]}]) == []
    o = OpenAICompatibleTransport("https://x/v1")
    assert [c.id for c in o.dangling_tool_calls([{"role": "assistant", "content": {
        "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}])] == ["c1"]
    assert o.dangling_tool_calls([{"role": "assistant", "content": {"content": "hi"}}]) == []
    assert ScriptedTransport([]).dangling_tool_calls([{"role": "assistant", "content": "x"}]) == []
