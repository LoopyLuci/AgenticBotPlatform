"""bot/browser_gateway.py: prompt flattening, tool-call emulation, and the OpenAI-compatible surface
(bot/dashboard/browser_gateway_api.py) end to end against a fake extension speaking protocol v1."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets

from bot import browser_bridge as bb, browser_gateway as gw
from bot.dashboard.server import build_app

TOKEN = "gateway-test-token"
H = {"X-Dashboard-Token": TOKEN}
EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
ORIGIN = f"chrome-extension://{EXT_ID}"


# ------------------------------------------------------------------------------------------ pure logic
def test_flatten_keeps_system_and_newest_turns_and_notes_what_was_dropped():
    messages = [{"role": "system", "content": "Be terse."}] + [{"role": "user", "content": f"turn {i}"} for i in range(50)]
    prompt = gw.flatten(messages, max_chars=500)
    assert prompt.startswith("Instructions:\nBe terse.")
    assert "turn 49" in prompt and prompt.endswith("Assistant:")
    assert "omitted" in prompt and "turn 0" not in prompt


def test_flatten_renders_tool_protocol_and_assistant_tool_calls():
    tools = [{"type": "function", "function": {"name": "search", "description": "look something up", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "find x"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search", "arguments": '{"q":"x"}'}}]},
                {"role": "tool", "name": "search", "content": "result: x=1"}]
    prompt = gw.flatten(messages, tools)
    assert "You can call tools" in prompt and "search" in prompt
    assert '<abp_tool name="search">{"q":"x"}</abp_tool>' in prompt
    assert "Tool result (search): result: x=1" in prompt


def test_parse_tool_calls_well_formed():
    tools = [{"function": {"name": "search"}}]
    text = 'Sure, let me check.\n<abp_tool name="search">{"q": "weather"}</abp_tool>'
    outside, calls, problem = gw.parse_tool_calls(text, tools)
    assert problem is None
    assert outside == "Sure, let me check."
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "search"
    assert json.loads(calls[0]["function"]["arguments"]) == {"q": "weather"}


def test_parse_tool_calls_unknown_tool_is_a_problem_not_silently_dropped():
    outside, calls, problem = gw.parse_tool_calls('<abp_tool name="nope">{}</abp_tool>', [{"function": {"name": "search"}}])
    assert calls == [] and problem is not None and "nope" in problem


def test_parse_tool_calls_bad_json_is_a_problem():
    outside, calls, problem = gw.parse_tool_calls('<abp_tool name="search">{not json}</abp_tool>', [{"function": {"name": "search"}}])
    assert calls == [] and problem is not None


def test_parse_tool_calls_plain_text_has_no_problem():
    outside, calls, problem = gw.parse_tool_calls("just a normal answer", [{"function": {"name": "search"}}])
    assert outside == "just a normal answer" and calls == [] and problem is None


def test_split_model_and_completion_body_shape():
    assert gw.split_model("web/grok") == ("web", "grok")
    assert gw.split_model("auto") == ("auto", "")
    body = gw.completion_body("web/grok", "hi there", [], "prompt text")
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["estimated"] is True


def test_register_providers_is_idempotent(tmp_path, monkeypatch):
    from bot import providers
    monkeypatch.setattr(providers, "_manager", providers.ConfigManager(path=tmp_path / "providers.yaml", missing_ok=True))
    r1 = gw.register_providers(8787)
    assert set(r1["added"]) == {"web", "browser-local"}
    r2 = gw.register_providers(8787)
    assert r2["added"] == []                                          # already there: nothing re-added
    assert providers.get_provider("web")["base_url"].endswith("/api/browser/v1/web")


# ------------------------------------------------------------------------------------------ end to end, against a fake extension
class FakeExtension:
    def __init__(self, base, key, handlers):
        self.url = base.replace("http", "ws") + "/api/browser/ws"
        self.key, self.handlers = key, handlers
        self.ws = None
        self._task = None

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, origin=ORIGIN)
        await self.ws.send(json.dumps({"v": 1, "id": "h1", "method": "hello", "params": {
            "protocol": [1], "key": self.key, "ext": {"name": "ABP Bridge", "version": "0.1.0", "browser": "Edge"}, "capabilities": {}}}))
        hello = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        assert "result" in hello, hello
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
        await self.ws.close()

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "method" in msg and msg.get("id"):
                    asyncio.create_task(self._answer(msg))
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _answer(self, msg):
        h = self.handlers.get(msg["method"])
        try:
            if h is None:
                await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "error": {"code": "E_METHOD", "message": "no"}}))
                return
            res = h(msg["params"])
            if asyncio.iscoroutine(res):
                res = await res
            await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "result": res}))
        except bb.BridgeError as e:
            await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "error": e.to_error()}))
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass


def _pair(base) -> str:
    code = httpx.post(f"{base}/api/browser/pair/code", headers=H).json()["code"]
    r = httpx.post(f"{base}/api/browser/pair/complete", json={"code": code, "browser": "Edge", "version": "0.1"}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    return r.json()["key"]


@pytest.fixture
def server(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    bb.bridge.connections.clear()
    bb.bridge._orphans.clear()
    bb.pairing = bb.Pairing()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def run(coro):
    return asyncio.run(coro)


def test_chat_completions_against_a_web_model(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"web.prompt": lambda p: {"text": "hello from grok"}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                             json={"model": "web/grok", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["choices"][0]["message"]["content"] == "hello from grok"
            assert body["model"] == "web/grok"
    run(go())


def test_chat_completions_accepts_bearer_auth_like_a_real_openai_client(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"web.prompt": lambda p: {"text": "ok"}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"model": "web/grok", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200, r.text
    run(go())


def test_chat_completions_streams_sse_deltas(server):
    key = _pair(server)

    async def go():
        async def handler(p):
            return {"text": "final answer"}

        async with FakeExtension(server, key, {"web.prompt": handler}), httpx.AsyncClient(timeout=10) as c:
            async with c.stream("POST", f"{server}/api/browser/v1/chat/completions", headers=H,
                                json={"model": "web/grok", "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as r:
                assert r.status_code == 200
                lines = [ln async for ln in r.aiter_lines() if ln.startswith("data: ")]
                assert lines[-1] == "data: [DONE]"
                data_lines = [json.loads(ln[len("data: "):]) for ln in lines[:-1]]
                text = "".join(d["choices"][0]["delta"].get("content", "") for d in data_lines)
                assert text == "final answer"
                assert data_lines[-1]["choices"][0]["finish_reason"] == "stop"
    run(go())


def test_tool_call_round_trip_and_repair(server):
    key = _pair(server)
    calls = {"n": 0}

    async def go():
        def prompt(p):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"text": 'ok<abp_tool name="search">{"q":"weather"}</abp_tool>'}
            return {"text": "should not be reached"}

        async with FakeExtension(server, key, {"web.prompt": prompt}), httpx.AsyncClient(timeout=10) as c:
            tools = [{"type": "function", "function": {"name": "search", "description": "look up", "parameters": {"type": "object"}}}]
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                             json={"model": "web/grok", "tools": tools, "messages": [{"role": "user", "content": "weather?"}]})
            assert r.status_code == 200, r.text
            msg = r.json()["choices"][0]["message"]
            assert msg["tool_calls"][0]["function"]["name"] == "search"
            assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "weather"}
            assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    run(go())


def test_malformed_tool_reply_triggers_one_repair_then_gives_up_gracefully(server):
    key = _pair(server)
    seen = []

    async def go():
        def prompt(p):
            seen.append(p["prompt"])
            if len(seen) == 1:
                return {"text": '<abp_tool name="ghost">{}</abp_tool>'}         # unknown tool: a problem
            return {"text": "here is a plain answer instead"}

        async with FakeExtension(server, key, {"web.prompt": prompt}), httpx.AsyncClient(timeout=10) as c:
            tools = [{"type": "function", "function": {"name": "search", "description": "look up", "parameters": {"type": "object"}}}]
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                             json={"model": "web/grok", "tools": tools, "messages": [{"role": "user", "content": "x"}]})
            assert r.status_code == 200
            msg = r.json()["choices"][0]["message"]
            assert not msg.get("tool_calls")
            assert msg["content"] == "here is a plain answer instead"
            assert len(seen) == 2 and "could not be used" in seen[1]
    run(go())


def test_web_model_refuses_sensitive_requests(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"web.prompt": lambda p: {"text": "should never run"}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers={**H, "X-ABP-Sensitive": "1"},
                             json={"model": "web/grok", "messages": [{"role": "user", "content": "secret"}]})
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "egress_blocked"
    run(go())


def test_not_connected_maps_to_503(server):
    r = httpx.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                   json={"model": "web/grok", "messages": [{"role": "user", "content": "hi"}]}, timeout=10)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "e_not_connected"


def test_unconfigured_provider_is_404(server):
    r = httpx.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                   json={"model": "no-such-provider/some-model", "messages": [{"role": "user", "content": "hi"}]}, timeout=10)
    assert r.status_code == 404


def test_no_auth_is_refused(server):
    r = httpx.post(f"{server}/api/browser/v1/chat/completions", json={"model": "web/grok", "messages": []})
    assert r.status_code == 401


def test_models_lists_connected_web_adapters(server):
    key = _pair(server)

    async def go():
        adapters = {"adapters": [{"id": "grok", "enabled": True, "models": ["fast"], "vision": False, "degraded": False, "logged_in": True}]}
        async with FakeExtension(server, key, {"web.adapters.list": lambda p: adapters}), httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{server}/api/browser/v1/models", headers=H)
            assert r.status_code == 200
            ids = {m["id"] for m in r.json()["data"]}
            assert {"web/grok", "web/grok:fast"} <= ids
    run(go())


# ------------------------------------------------------------------------------------------ browser-local (in-browser models, P3)
def test_browser_local_chat_completion(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"llm.generate": lambda p: {"text": "hello from the local model"}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                             json={"model": "browser-local/qwen2.5-1.5b", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200, r.text
            assert r.json()["choices"][0]["message"]["content"] == "hello from the local model"
    run(go())


def test_browser_local_streams_deltas(server):
    key = _pair(server)

    async def go():
        async def handler(p):
            return {"text": "streamed answer"}

        async with FakeExtension(server, key, {"llm.generate": handler}), httpx.AsyncClient(timeout=10) as c:
            async with c.stream("POST", f"{server}/api/browser/v1/chat/completions", headers=H,
                                json={"model": "browser-local/qwen2.5-1.5b", "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as r:
                assert r.status_code == 200
                lines = [ln async for ln in r.aiter_lines() if ln.startswith("data: ")]
                assert lines[-1] == "data: [DONE]"
                data_lines = [json.loads(ln[len("data: "):]) for ln in lines[:-1]]
                text = "".join(d["choices"][0]["delta"].get("content", "") for d in data_lines)
                assert text == "streamed answer"
    run(go())


def test_browser_local_no_engine_yet_maps_to_503(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {}), httpx.AsyncClient(timeout=10) as c:      # no llm.generate handler at all: the fake answers E_METHOD
            r = await c.post(f"{server}/api/browser/v1/chat/completions", headers=H,
                             json={"model": "browser-local/qwen2.5-1.5b", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "model_unavailable"
    run(go())


def test_embeddings_end_to_end(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"llm.embed": lambda p: {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/v1/embeddings", headers=H,
                             json={"model": "browser-local/bge-small", "input": ["hello", "world"]})
            assert r.status_code == 200, r.text
            body = r.json()
            assert [d["embedding"] for d in body["data"]] == [[0.1, 0.2], [0.3, 0.4]]
            assert body["data"][0]["index"] == 0 and body["data"][1]["index"] == 1
    run(go())


def test_embeddings_reject_non_browser_local_model(server):
    r = httpx.post(f"{server}/api/browser/v1/embeddings", headers=H, json={"model": "openai/text-embedding-3-small", "input": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_model"


def test_models_catalog_route_empty_when_not_connected(server):
    r = httpx.get(f"{server}/api/browser/models/catalog", headers=H)
    assert r.status_code == 200
    assert r.json()["models"] == []


def test_models_catalog_route_reaches_the_extension(server):
    key = _pair(server)

    async def go():
        catalog = {"models": [{"id": "bge-small", "task": "embed", "fit": "good", "installed": False}]}
        async with FakeExtension(server, key, {"llm.catalog": lambda p: catalog}), httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{server}/api/browser/models/catalog", headers=H)
            assert r.status_code == 200
            assert r.json()["models"] == catalog["models"]
    run(go())


def test_models_load_route(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key, {"llm.load": lambda p: {"ok": True}}), httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{server}/api/browser/models/bge-small/load", headers=H)
            assert r.status_code == 200
            assert r.json() == {"ok": True}
    run(go())
