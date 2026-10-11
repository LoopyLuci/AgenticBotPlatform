"""Dashboard/desktop chat streaming: StreamEvents from the native loop become coalesced chat_stream frames."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, StreamEvent, ToolCall
from bot.backends.base import BackendError
from bot.backends.native_backend import NativeAgentBackend
from bot.dashboard.chat_stream import ChatStreamer

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _streamer(frames, interval=0.02):
    async def send(payload):
        frames.append(payload)

    return ChatStreamer(send, 7, "chat-1", "turn-1", flush_interval_s=interval)


def test_text_deltas_are_coalesced_and_ordered_with_reset_and_done():
    frames = []

    async def go():
        s = _streamer(frames)
        for piece in ["a", "b", "c"]:
            await s.notify(StreamEvent("text", piece))
        await asyncio.sleep(0.1)            # one timed flush for the burst
        await s.notify(StreamEvent("text", "d"))
        await s.notify(StreamEvent("reset"))  # flushes "d" first, then resets
        await s.notify(StreamEvent("text", "e"))
        await s.finish()

    _run(go())
    assert [(f["kind"], f.get("text")) for f in frames] == [
        ("text_delta", "abc"), ("text_delta", "d"), ("reset", None), ("text_delta", "e"), ("done", None)]
    assert all(f["type"] == "chat_stream" and f["instance_id"] == 7 and f["chat_id"] == "chat-1"
               and f["turn_id"] == "turn-1" for f in frames)


def test_a_burst_of_many_deltas_makes_few_frames():
    frames = []

    async def go():
        s = _streamer(frames, interval=0.05)
        for _ in range(200):
            await s.notify(StreamEvent("text", "x"))
        await s.finish()

    _run(go())
    deltas = [f for f in frames if f["kind"] == "text_delta"]
    assert "".join(f["text"] for f in deltas) == "x" * 200 and len(deltas) <= 3
    assert frames[-1]["kind"] == "done"


def test_a_failing_broadcaster_never_raises():
    async def send(payload):
        raise RuntimeError("socket gone")

    async def go():
        s = ChatStreamer(send, 1, "c", "t", flush_interval_s=0.01)
        await s.notify(StreamEvent("text", "hi"))
        await s.notify(StreamEvent("tool_start", "read_file"))
        await s.finish()

    _run(go())


class _ToolThenText(ProviderTransport):
    supports_streaming = True

    def __init__(self, fail_text=False):
        self.calls = 0

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": "tool output"}]

    async def send_stream(self, *, on_event, **kw):
        self.calls += 1
        if self.calls == 1:
            call = ToolCall("t1", "read_file", {"path": "TOP-SECRET-ARG.txt"})
            return NormalizedResponse(text="", tool_calls=[call],
                                      assistant_message={"role": "assistant", "content": "calling"})
        await on_event(StreamEvent("text", "all "))
        await on_event(StreamEvent("text", "done"))
        return NormalizedResponse(text="all done", tokens=3, assistant_message={"role": "assistant", "content": "all done"})

    async def send(self, **kw):
        raise AssertionError("must stream")


def test_loop_events_reach_the_broadcaster_with_tool_names_but_no_arguments(temp_db, tmp_path):
    frames = []

    async def go():
        s = _streamer(frames)
        backend = NativeAgentBackend(_ToolThenText(), model="m")
        result = await backend.ask("hi", context={"cwd": str(tmp_path), "stream_notify": s.notify})
        await s.finish()
        return result

    result = _run(go())
    assert result.text == "all done"
    kinds = [f["kind"] for f in frames]
    assert kinds == ["tool_start", "tool_end", "text_delta", "done"]
    assert [f.get("tool") for f in frames[:2]] == ["read_file", "read_file"]
    assert "TOP-SECRET-ARG" not in json.dumps(frames)


def test_failover_reset_reaches_the_broadcaster(temp_db, tmp_path, monkeypatch):
    from bot.backends import native_backend

    class Dies(_ToolThenText):
        async def send_stream(self, *, on_event, **kw):
            await on_event(StreamEvent("text", "par"))
            raise BackendError("stream died")

    class Good(_ToolThenText):
        async def send_stream(self, *, on_event, **kw):
            await on_event(StreamEvent("text", "fresh"))
            return NormalizedResponse(text="fresh", assistant_message={"role": "assistant", "content": "fresh"})

    monkeypatch.setattr(native_backend, "_resolve_fallback_transport", lambda instance_id: (Good(), "fb"))
    frames = []

    async def go():
        s = _streamer(frames)
        r = await NativeAgentBackend(Dies(), model="m").ask(
            "hi", context={"cwd": str(tmp_path), "stream_notify": s.notify, "instance_id": 1})
        await s.finish()
        return r

    assert _run(go()).text == "fresh"
    assert [(f["kind"], f.get("text")) for f in frames] == [
        ("text_delta", "par"), ("reset", None), ("text_delta", "fresh"), ("done", None)]


def test_a_notify_that_raises_does_not_fail_the_turn(temp_db, tmp_path):
    async def boom(event):
        raise RuntimeError("client went away")

    result = _run(NativeAgentBackend(_ToolThenText(), model="m").ask(
        "hi", context={"cwd": str(tmp_path), "stream_notify": boom}))
    assert result.text == "all done"


def test_front_ends_render_chat_stream_without_innerhtml():
    for rel in ("bot/dashboard/static/dashboard.js", "desktop-app/ui/main.js"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "msg.type === 'chat_stream'" in src, rel
        m = re.search(r"function onChatStream\(msg\) \{.*?\n\}\n", src, re.S)
        assert m, rel
        body = m.group(0)
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, rel
        assert "textContent" in body, rel
