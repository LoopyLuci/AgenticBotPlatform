"""Streaming: the shared event model, the two real implementations against faked
wire formats, and the loop's handling of a stream callback."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from bot.agent_runtime.transports import anthropic as anth
from bot.agent_runtime.transports import openai_compatible as oai
from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, StreamEvent
from bot.backends import native_backend
from bot.backends.base import BackendError
from bot.backends.native_backend import NativeAgentBackend


def _run(coro):
    return asyncio.run(coro)


async def _collect(transport, **kw):
    events = []

    async def on_event(e):
        events.append(e)

    resp = await transport.send_stream(on_event=on_event, model="m", history=[{"role": "user", "content": "hi"}],
                                       tool_schemas=[], max_tokens=50, timeout_s=5, **kw)
    return resp, events


# ---- default -------------------------------------------------------------------
def test_the_default_send_stream_delivers_the_whole_reply_once():
    class Plain(ProviderTransport):
        async def send(self, **kw):
            return NormalizedResponse(text="all at once", assistant_message={"role": "assistant", "content": "all at once"})

    resp, events = _run(_collect(Plain()))
    assert resp.text == "all at once" and [(e.kind, e.text) for e in events] == [("text", "all at once")]
    assert not Plain.supports_streaming


# ---- OpenAI-compatible SSE -----------------------------------------------------
def _sse(*chunks) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _install_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(oai.httpx, "AsyncClient",
                        lambda *, timeout: real(transport=httpx.MockTransport(handler), timeout=timeout))


def test_openai_sse_text_deltas_are_streamed_and_usage_is_read(monkeypatch):
    body = _sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    _install_http(monkeypatch, handler)
    resp, events = _run(_collect(oai.OpenAICompatibleTransport("https://x/v1", api_key="unused")))
    assert [e.text for e in events] == ["Hel", "lo"]
    assert resp.text == "Hello" and resp.tokens == 12 and not resp.tool_calls
    assert seen["payload"]["stream"] is True and seen["payload"]["stream_options"] == {"include_usage": True}


def test_openai_sse_tool_call_fragments_are_assembled(monkeypatch):
    body = _sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "list_dir", "arguments": "{}"}}]}}]},
    )
    _install_http(monkeypatch, lambda r: httpx.Response(200, content=body))
    resp, events = _run(_collect(oai.OpenAICompatibleTransport("https://x/v1")))
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == [
        ("c1", "read_file", {"path": "a.txt"}), ("c2", "list_dir", {})]
    assert events == []
    stored = resp.assistant_message["content"]["tool_calls"]
    assert stored[0]["function"]["arguments"] == '{"path": "a.txt"}'


def test_openai_sse_retries_once_without_stream_options_when_the_server_rejects_it(monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append("stream_options" in payload)
        if "stream_options" in payload:
            return httpx.Response(400, json={"error": "unknown parameter: stream_options"})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "ok"}}]}))

    _install_http(monkeypatch, handler)
    resp, _ = _run(_collect(oai.OpenAICompatibleTransport("https://x/v1")))
    assert resp.text == "ok" and calls == [True, False]


def test_openai_sse_http_errors_become_backend_errors(monkeypatch):
    _install_http(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(BackendError, match="500"):
        _run(_collect(oai.OpenAICompatibleTransport("https://x/v1")))


def test_openai_non_streaming_send_is_unchanged(monkeypatch):
    def handler(request):
        assert "stream" not in json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "plain"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    _install_http(monkeypatch, handler)
    resp = _run(oai.OpenAICompatibleTransport("https://x/v1").send(
        model="m", history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=5, timeout_s=5))
    assert resp.text == "plain" and resp.tokens == 2


# ---- Anthropic -----------------------------------------------------------------
class _FakeStream:
    def __init__(self, pieces, final):
        self._pieces, self._final = pieces, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for p in self._pieces:
                yield p
        return gen()

    async def get_final_message(self):
        return self._final


def _anthropic_with(final, pieces):
    t = anth.AnthropicTransport(api_key="unused")
    t._client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _FakeStream(pieces, final)))
    return t


def test_anthropic_stream_delivers_text_and_returns_the_assembled_message():
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Hello there")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=7, output_tokens=3, cache_creation_input_tokens=0, cache_read_input_tokens=5))
    resp, events = _run(_collect(_anthropic_with(final, ["Hello", " there"])))
    assert [e.text for e in events] == ["Hello", " there"]
    assert resp.text == "Hello there" and resp.tokens == 10 and resp.cache_read_tokens == 5
    assert resp.assistant_message == {"role": "assistant", "content": [{"type": "text", "text": "Hello there"}]}


def test_anthropic_stream_keeps_tool_calls():
    final = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"path": "a"})],
        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    resp, events = _run(_collect(_anthropic_with(final, [])))
    assert [(c.id, c.name) for c in resp.tool_calls] == [("t1", "read_file")] and events == []


def test_anthropic_stream_failures_become_backend_errors():
    class Boom:
        def stream(self, **kw):
            raise RuntimeError("no network")

    t = anth.AnthropicTransport(api_key="unused")
    t._client = SimpleNamespace(messages=Boom())
    with pytest.raises(BackendError, match="no network"):
        _run(_collect(t))


# ---- the loop ------------------------------------------------------------------
class _StreamingScripted(ProviderTransport):
    supports_streaming = True

    def __init__(self, pieces, fail=False):
        self.pieces, self.fail = pieces, fail

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return []

    async def send_stream(self, *, on_event, **kw):
        for p in self.pieces:
            await on_event(StreamEvent("text", p))
        if self.fail:
            raise BackendError("stream died")
        text = "".join(self.pieces)
        return NormalizedResponse(text=text, tokens=3, assistant_message={"role": "assistant", "content": text})

    async def send(self, **kw):
        raise AssertionError("send() must not be used when streaming was asked for")


def test_the_loop_streams_when_a_callback_is_given(temp_db, tmp_path):
    got = []

    async def notify(event):
        got.append(event.text)

    backend = NativeAgentBackend(_StreamingScripted(["a", "b", "c"]), model="m")
    result = _run(backend.ask("hi", context={"cwd": str(tmp_path), "stream_notify": notify}))
    assert got == ["a", "b", "c"] and result.text == "abc"


def test_the_loop_does_not_stream_without_a_callback(temp_db, tmp_path):
    class Plain(_StreamingScripted):
        async def send(self, **kw):
            return NormalizedResponse(text="plain", assistant_message={"role": "assistant", "content": "plain"})

    result = _run(NativeAgentBackend(Plain(["x"]), model="m").ask("hi", context={"cwd": str(tmp_path)}))
    assert result.text == "plain"


def test_a_failing_callback_never_breaks_the_turn(temp_db, tmp_path):
    async def notify(event):
        raise RuntimeError("client went away")

    result = _run(NativeAgentBackend(_StreamingScripted(["ok"]), model="m").ask(
        "hi", context={"cwd": str(tmp_path), "stream_notify": notify}))
    assert result.text == "ok"


def test_a_transport_that_cannot_stream_is_not_asked_to(temp_db, tmp_path):
    class NoStream(ProviderTransport):
        supports_streaming = False

        def user_message(self, text, *, images=None, documents=None):
            return {"role": "user", "content": text}

        async def send(self, **kw):
            return NormalizedResponse(text="whole", assistant_message={"role": "assistant", "content": "whole"})

        async def send_stream(self, **kw):
            raise AssertionError("must not stream")

    async def notify(event):
        raise AssertionError("nothing to stream")

    result = _run(NativeAgentBackend(NoStream(), model="m").ask(
        "hi", context={"cwd": str(tmp_path), "stream_notify": notify}))
    assert result.text == "whole"


def test_failover_tells_the_client_to_discard_what_it_already_showed(temp_db, tmp_path, monkeypatch):
    events = []

    async def notify(event):
        events.append((event.kind, event.text))

    fallback = _StreamingScripted(["fresh"])
    monkeypatch.setattr(native_backend, "_resolve_fallback_transport", lambda instance_id: (fallback, "fb-model"))
    backend = NativeAgentBackend(_StreamingScripted(["par", "tial"], fail=True), model="m")
    result = _run(backend.ask("hi", context={"cwd": str(tmp_path), "stream_notify": notify, "instance_id": 1}))
    assert events == [("text", "par"), ("text", "tial"), ("reset", ""), ("text", "fresh")]
    assert result.text == "fresh"


def test_the_trace_records_that_a_call_streamed(temp_db, tmp_path):
    from bot.agent_runtime import trace

    async def notify(event):
        pass

    result = _run(NativeAgentBackend(_StreamingScripted(["x"]), model="m").ask(
        "hi", context={"cwd": str(tmp_path), "stream_notify": notify}))
    llm = [e for e in trace.run_events(result.raw["trace_run"]) if e["kind"] == "llm.call"]
    assert llm and llm[0]["data"]["streamed"] == 1
