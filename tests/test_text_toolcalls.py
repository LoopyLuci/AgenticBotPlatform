"""Tool calls written as text (text_toolcalls.py), their use in the OpenAI-compatible transport (one corrective
re-ask, native calls untouched, stripped content, grammar/text modes), per-model counters, and the context window a
ModelMistress model is loaded with (read from a fake hub, cached, never the 128k default)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from bot.agent_runtime import context_window as cw
from bot.agent_runtime import text_toolcalls as tt
from bot.agent_runtime.transports.base import StreamEvent
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
from bot.backends.base import BackendError
from bot.modelmistress import client as mm

TOOLS = [
    {"name": "read_file", "description": "Read a file",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "run_tests", "description": "Run tests",
     "input_schema": {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}},
                                                       "verbose": {"type": "boolean"}}}},
    {"name": "ping", "description": "No arguments", "input_schema": {"type": "object", "properties": {}}},
]


@pytest.fixture(autouse=True)
def _clean():
    tt.reset()
    cw.forget_calibration()
    mm.forget_context()
    yield
    tt.reset()
    mm.forget_context()


def names(r):
    return [(c.name, c.arguments) for c in r.calls]


# ---- every format ---------------------------------------------------------------------------------
def test_qwen_hermes_tool_call_json():
    r = tt.parse('I will read it.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>', TOOLS)
    assert names(r) == [("read_file", {"path": "a.py"})]
    assert r.text == "I will read it."
    assert not r.errors and not r.repaired


def test_two_tool_call_blocks():
    r = tt.parse('<tool_call>{"name":"ping","arguments":{}}</tool_call><tool_call>{"name":"read_file","arguments":{"path":"x"}}</tool_call>', TOOLS)
    assert [c.name for c in r.calls] == ["ping", "read_file"]
    assert r.text == ""


def test_function_equals_style_with_parameters():
    r = tt.parse("<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n<parameter=limit>\n20\n</parameter>\n</function>\n</tool_call>", TOOLS)
    assert names(r) == [("read_file", {"path": "src/a.py", "limit": 20})]    # "20" coerced to the integer the schema wants
    assert r.text == ""


def test_function_equals_without_wrapper_and_with_json_body():
    r = tt.parse('Sure. <function=read_file>{"path": "b.py"}</function>', TOOLS)
    assert names(r) == [("read_file", {"path": "b.py"})]
    assert r.text == "Sure."


def test_bare_json_object():
    r = tt.parse('{"name": "read_file", "arguments": {"path": "c.py"}}', TOOLS)
    assert names(r) == [("read_file", {"path": "c.py"})]
    assert r.text == ""


def test_bare_json_variants_parameters_and_openai_function_wrapper_and_string_arguments():
    for text in ('{"name": "ping", "parameters": {}}',
                 '{"type": "function", "function": {"name": "ping", "arguments": "{}"}}',
                 '{"tool_calls": [{"function": {"name": "ping", "arguments": {}}}]}',
                 '[TOOL_CALLS] [{"name": "ping", "arguments": {}}]'):
        r = tt.parse(text, TOOLS)
        assert names(r) == [("ping", {})], text
        assert r.text == "", text


def test_bare_json_array_of_calls_in_prose():
    r = tt.parse('Doing both:\n[{"name": "ping", "arguments": {}}, {"name": "read_file", "arguments": {"path": "z"}}]\nDone.', TOOLS)
    assert [c.name for c in r.calls] == ["ping", "read_file"]
    assert r.text == "Doing both:\n\nDone."


def test_fenced_json_block():
    r = tt.parse('Let me look.\n```json\n{"name": "read_file", "arguments": {"path": "d.py"}}\n```\nThen I will continue.', TOOLS)
    assert names(r) == [("read_file", {"path": "d.py"})]
    assert "```" not in r.text and r.text.startswith("Let me look.") and r.text.endswith("Then I will continue.")


def test_prose_json_that_is_not_a_call_is_left_alone():
    for text in ('The config is {"debug": true, "level": 3}.', "Use a list like [1, 2, 3].", "No call here."):
        r = tt.parse(text, TOOLS)
        assert not r.attempted and not r.calls and r.text == text


# ---- repair ------------------------------------------------------------------------------------------
def test_repairs_trailing_comma_single_quotes_and_raw_newline():
    r = tt.parse("<tool_call>{'name': 'read_file', 'arguments': {'path': 'a.py',},}</tool_call>", TOOLS)
    assert names(r) == [("read_file", {"path": "a.py"})] and r.repaired
    r = tt.parse('<tool_call>{"name": "read_file", "arguments": {"path": "line1\nline2"}}</tool_call>', TOOLS)
    assert names(r) == [("read_file", {"path": "line1\nline2"})] and r.repaired


def test_repairs_python_literals_unquoted_keys_and_bad_escape():
    r = tt.parse('<tool_call>{name: "run_tests", arguments: {verbose: True, files: ["tests\\data\\a.py"]}}</tool_call>', TOOLS)
    assert names(r) == [("run_tests", {"verbose": True, "files": ["tests\\data\\a.py"]})]


def test_light_coercion_to_the_schema():
    r = tt.parse('{"name": "run_tests", "arguments": {"files": "[\\"a.py\\", \\"b.py\\"]", "verbose": "true"}}', TOOLS)
    assert names(r) == [("run_tests", {"files": ["a.py", "b.py"], "verbose": True})]
    r = tt.parse('{"name": "read_file", "arguments": {"path": "a", "limit": "7"}}', TOOLS)
    assert r.calls[0].arguments["limit"] == 7


# ---- rejection ---------------------------------------------------------------------------------------
def test_unknown_tool_name_rejected():
    r = tt.parse('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>', TOOLS)
    assert r.attempted and not r.calls and r.errors and "rm_rf" in r.errors[0]
    assert r.text.startswith("<tool_call>")                # nothing is stripped when nothing is accepted


def test_invalid_arguments_rejected():
    for text, expect in (('{"name": "read_file", "arguments": {}}', "path"),                       # missing required
                         ('{"name": "read_file", "arguments": {"path": ["x"]}}', "path"),           # wrong type
                         ('{"name": "read_file", "arguments": {"path": "a", "limit": "many"}}', "limit")):
        r = tt.parse(text, TOOLS)
        assert r.attempted and not r.calls and r.errors, text
        assert expect in r.errors[0], r.errors


def test_one_bad_call_means_no_call_runs():
    r = tt.parse('<tool_call>{"name":"ping","arguments":{}}</tool_call><tool_call>{"name":"nope","arguments":{}}</tool_call>', TOOLS)
    assert not r.calls and len(r.errors) == 1


def test_unterminated_and_unparseable_calls_never_run():
    r = tt.parse('<tool_call>{"name": "ping", "arguments": {}}', TOOLS)
    assert r.attempted and not r.calls and "never closed" in r.errors[0]
    r = tt.parse("<function=ping>", TOOLS)
    assert r.attempted and not r.calls
    r = tt.parse('<tool_call>{"name": "ping", "arguments": {</tool_call>', TOOLS)
    assert r.attempted and not r.calls and "not valid JSON" in r.errors[0]


def test_no_tools_offered_means_no_parsing():
    assert not tt.parse('{"name": "ping", "arguments": {}}', []).attempted


# ---- the transport -----------------------------------------------------------------------------------
class FakeServer:
    """Stands in for OpenAICompatibleTransport._post: answers with queued message dicts and keeps the payloads."""

    def __init__(self, *messages, usage=(10, 5)):
        self.queue = list(messages)
        self.payloads: list[dict] = []
        self.usage = usage

    async def __call__(self, payload, headers, timeout_s):
        self.payloads.append(json.loads(json.dumps(payload)))
        message = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]}}


def make(monkeypatch, *messages, mode="native"):
    t = OpenAICompatibleTransport(base_url="http://127.0.0.1:1/v1")
    t.router_provider = "fake"
    server = FakeServer(*messages)
    monkeypatch.setattr(t, "_post", server)
    monkeypatch.setattr(tt, "mode_for", lambda model, provider="", transport=None: mode)
    return t, server


def send(t, tools=TOOLS):
    return asyncio.run(t.send(model="m", history=[{"role": "user", "content": "go"}], tool_schemas=tools, max_tokens=100, timeout_s=5))


CALL_TEXT = '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'


def test_native_tool_calls_pass_through_unchanged(monkeypatch):
    native = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}]
    t, server = make(monkeypatch, {"content": "hm " + CALL_TEXT, "tool_calls": native})
    r = send(t)
    assert [(c.id, c.name, c.arguments) for c in r.tool_calls] == [("c1", "read_file", {"path": "a"})]
    assert r.text == "hm " + CALL_TEXT                      # native path: the content is not touched
    assert r.assistant_message["content"]["tool_calls"] == native
    assert len(server.payloads) == 1 and "tools" in server.payloads[0]
    assert tt.stats("fake/m") == {"native": 1, "text": 0, "repaired": 0, "failed": 0}


def test_text_call_becomes_a_call_with_stripped_content(monkeypatch):
    t, server = make(monkeypatch, {"content": "Reading.\n" + CALL_TEXT})
    r = send(t)
    assert [(c.name, c.arguments) for c in r.tool_calls] == [("read_file", {"path": "a.py"})]
    assert r.text == "Reading."
    stored = r.assistant_message["content"]
    assert stored["content"] == "Reading." and stored["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(stored["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}
    assert stored["tool_calls"][0]["id"] == r.tool_calls[0].id and r.tool_calls[0].id
    assert r.stop is False and len(server.payloads) == 1
    assert tt.stats("fake/m")["text"] == 1 and tt.stats("fake/m")["failed"] == 0


def test_repaired_json_is_counted_as_repaired(monkeypatch):
    t, _ = make(monkeypatch, {"content": "<tool_call>{'name': 'ping', 'arguments': {},}</tool_call>"})
    assert [c.name for c in send(t).tool_calls] == ["ping"]
    assert tt.stats("fake/m") == {"native": 0, "text": 0, "repaired": 1, "failed": 0}


def test_unusable_call_is_reasked_once_and_then_works(monkeypatch):
    t, server = make(monkeypatch, {"content": '<tool_call>{"name": "read_files", "arguments": {}}</tool_call>'},
                     {"content": CALL_TEXT})
    r = send(t)
    assert [c.name for c in r.tool_calls] == ["read_file"]
    assert len(server.payloads) == 2
    followup = server.payloads[1]["messages"][-2:]
    assert followup[0]["role"] == "assistant" and "read_files" in followup[0]["content"]
    assert followup[1]["role"] == "user" and "read_files" in followup[1]["content"] and "read_file, run_tests, ping" in followup[1]["content"]
    assert r.tokens == 30                                     # both requests are paid for
    assert tt.stats("fake/m") == {"native": 0, "text": 0, "repaired": 1, "failed": 0}


def test_reask_happens_once_then_gives_up_without_looping(monkeypatch):
    bad = {"content": '<tool_call>{"name": "nope", "arguments": {}}</tool_call>'}
    t, server = make(monkeypatch, bad)
    r = send(t)
    assert r.tool_calls == [] and r.stop is True
    assert len(server.payloads) == 2                          # the original and exactly one re-ask
    assert "nope" in r.text                                   # given up cleanly: the reply is returned as it is
    assert tt.stats("fake/m")["failed"] == 1


def test_reask_answered_in_plain_text_ends_the_turn(monkeypatch):
    t, server = make(monkeypatch, {"content": '{"name": "nope", "arguments": {}}'}, {"content": "Sorry, here is the answer."})
    r = send(t)
    assert r.tool_calls == [] and r.text == "Sorry, here is the answer." and len(server.payloads) == 2
    assert tt.stats("fake/m")["failed"] == 1


def test_a_failed_reask_request_gives_up_cleanly(monkeypatch):
    t, _ = make(monkeypatch, {"content": '{"name": "nope", "arguments": {}}'})
    calls = []

    async def flaky(payload, headers, timeout_s):
        calls.append(1)
        if len(calls) > 1:
            raise BackendError("down")
        return {"choices": [{"message": {"content": '{"name": "nope", "arguments": {}}'}}]}

    monkeypatch.setattr(t, "_post", flaky)
    r = send(t)
    assert r.tool_calls == [] and len(calls) == 2


def test_plain_answers_and_no_tools_are_not_touched(monkeypatch):
    t, server = make(monkeypatch, {"content": "Just words with {braces} in them."})
    r = send(t)
    assert r.text == "Just words with {braces} in them." and r.tool_calls == [] and len(server.payloads) == 1
    t, server = make(monkeypatch, {"content": CALL_TEXT})
    assert send(t, tools=[]).tool_calls == [] and len(server.payloads) == 1


def test_streaming_replaces_the_shown_text_when_a_call_is_found(monkeypatch):
    t, _ = make(monkeypatch, {"content": "x"})
    from bot.agent_runtime.transports.base import NormalizedResponse

    async def fake_consume(self, client, url, payload, headers, on_event):
        await on_event(StreamEvent("text", "Reading. " + CALL_TEXT))
        return NormalizedResponse(text="Reading. " + CALL_TEXT, assistant_message={"role": "assistant", "content": {"content": "Reading. " + CALL_TEXT}})

    monkeypatch.setattr(OpenAICompatibleTransport, "_consume_stream", fake_consume)
    events = []

    async def on_event(e):
        events.append((e.kind, e.text))

    r = asyncio.run(t.send_stream(on_event=on_event, model="m", history=[{"role": "user", "content": "go"}],
                                  tool_schemas=TOOLS, max_tokens=50, timeout_s=5))
    assert [c.name for c in r.tool_calls] == ["read_file"]
    assert events == [("text", "Reading. " + CALL_TEXT), ("reset", ""), ("text", "Reading.")]


# ---- modes ----------------------------------------------------------------------------------------------
def test_native_mode_sends_tools_and_no_response_format(monkeypatch):
    t, server = make(monkeypatch, {"content": "ok"})
    send(t)
    p = server.payloads[0]
    assert "tools" in p and "response_format" not in p


def test_text_mode_describes_tools_in_the_prompt_instead(monkeypatch):
    t, server = make(monkeypatch, {"content": CALL_TEXT}, mode="text")
    r = send(t)
    p = server.payloads[0]
    assert "tools" not in p and "tool_choice" not in p and "response_format" not in p
    assert p["messages"][0]["role"] == "system" and "<tool_call>" in p["messages"][0]["content"] and "read_file" in p["messages"][0]["content"]
    assert [c.name for c in r.tool_calls] == ["read_file"]


def test_grammar_mode_sends_a_json_schema_and_reads_calls_and_final_answers(monkeypatch):
    t, server = make(monkeypatch, {"content": '{"name": "ping", "arguments": {}}'}, mode="grammar")
    r = send(t)
    rf = server.payloads[0]["response_format"]
    assert rf["type"] == "json_schema"
    options = rf["json_schema"]["schema"]["anyOf"]
    assert {o["properties"]["name"]["const"] for o in options} == {"final_answer", "read_file", "run_tests", "ping"}
    assert "tools" not in server.payloads[0]
    assert [c.name for c in r.tool_calls] == ["ping"]
    t, _ = make(monkeypatch, {"content": '{"name": "final_answer", "arguments": {"text": "All done."}}'}, mode="grammar")
    r = send(t)
    assert r.tool_calls == [] and r.text == "All done."
    assert r.assistant_message["content"] == {"content": "All done."}


def test_grammar_mode_falls_back_when_the_server_refuses_response_format(monkeypatch):
    t = OpenAICompatibleTransport(base_url="http://127.0.0.1:1/v1")
    monkeypatch.setattr(tt, "mode_for", lambda model, provider="", transport=None: "grammar")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format is not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name": "ping", "arguments": {}}'}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = send(t)
    assert [c.name for c in r.tool_calls] == ["ping"]
    assert "response_format" in seen[0] and "response_format" not in seen[1] and t._no_response_format
    send(t)
    assert "response_format" not in seen[-1]                  # remembered: not offered again


def test_mode_for_resolution(monkeypatch):
    cfg = {"native_agent": {"tool_call_modes": {"*": "text", "qwen": "grammar", "local/qwen3-8b": "native", "x": "bogus"}}}
    monkeypatch.setattr("bot.config.config", SimpleNamespace(current=cfg))
    assert tt.mode_for("llama3", "local") == "text"
    assert tt.mode_for("qwen2.5-7b", "local") == "grammar"
    assert tt.mode_for("qwen3-8b", "local") == "native"       # the longest match wins
    assert tt.mode_for("xyz", "local") == "text"              # an invalid value is ignored
    monkeypatch.setattr("bot.config.config", SimpleNamespace(current={}))
    assert tt.mode_for("qwen2.5-7b", "local") == "native"     # default


def test_counters_per_model():
    tt.record("a/m", "native", 2)
    tt.record("a/m", "failed")
    tt.record("b/m", "text")
    tt.record("b/m", "bogus")
    assert tt.stats("a/m") == {"native": 2, "text": 0, "repaired": 0, "failed": 1}
    assert tt.stats("zzz") == {"native": 0, "text": 0, "repaired": 0, "failed": 0}
    assert set(tt.stats()) == {"a/m", "b/m"}


# ---- context windows from a ModelMistress hub ----------------------------------------------------------
class FakeHub:
    """Answers the routes the window lookup uses; each can be switched off."""

    def __init__(self):
        self.loaded: object = [{"name": "qwen3-8b", "status": "ready", "context_length": 16384}]
        self.models: list = [{"id": "qwen3-8b", "metadata": {"context_length": 262144}}]
        self.props: object = None
        self.requests: list[str] = []

    def install(self, monkeypatch):
        hub = self

        def respond(method, url, **kw):
            hub.requests.append(url.split("127.0.0.1:9", 1)[-1])
            path = url.split("127.0.0.1:9", 1)[-1]
            if path.endswith("/v1/call/model.loaded"):
                if hub.loaded is None:
                    return httpx.Response(404, json={"error": "unknown op"})
                return httpx.Response(200, json={"result": hub.loaded})
            if path.endswith("/v1/models"):
                return httpx.Response(200, json={"data": hub.models})
            if path.startswith("/props") or path.startswith("/v1/props"):
                return httpx.Response(200, json=hub.props) if hub.props is not None else httpx.Response(404, json={})
            return httpx.Response(404, json={})

        monkeypatch.setattr(mm.httpx, "request", lambda method, url, **kw: respond(method, url, **kw))
        monkeypatch.setattr(mm.httpx, "get", lambda url, **kw: respond("GET", url, **kw))
        monkeypatch.setattr(mm, "find", lambda peer="", refresh=False: mm.Hub("http://127.0.0.1:9", "tok"))
        monkeypatch.setattr(mm, "find_cached", lambda: mm.Hub("http://127.0.0.1:9", "tok"))


@pytest.fixture
def hub(monkeypatch):
    h = FakeHub()
    h.install(monkeypatch)
    return h


@pytest.fixture
def conf(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(cw, "_cfg", lambda: values)
    return values


def warm(model: str = "qwen3-8b") -> None:
    """The agent loop never waits on the hub (it reads the cache and refreshes it in the background); a test that
    wants the hub's figure fills the cache first, the way the background refresh would."""
    mm.context_length(model, wait=True)


def test_the_agent_loop_never_waits_on_the_hub(hub, conf, monkeypatch):
    import threading
    gate = threading.Event()
    real = mm._lookup_context
    monkeypatch.setattr(mm, "_lookup_context", lambda h, m: (gate.wait(5), real(h, m))[1])
    assert cw.window_for("modelmistress/qwen3-8b") == cw.LOCAL_DEFAULT_WINDOW   # a slow hub: answered at once
    gate.set()
    for _ in range(100):                                                           # the refresh fills the cache
        if cw.window_for("modelmistress/qwen3-8b") == 16384:
            break
        threading.Event().wait(0.02)
    assert cw.window_for("modelmistress/qwen3-8b") == 16384


def test_window_from_the_hubs_loaded_model(hub, conf):
    warm()
    assert cw.window_for("modelmistress/qwen3-8b") == 16384
    assert cw.window_for("qwen3-8b", "modelmistress") == 16384
    assert mm.context_length("qwen3-8b") == 16384


def test_window_from_a_0_3_hubs_serving_block(hub, conf):
    # MM 0.3.0's model.loaded nests the window under "serving"; a request gets n_ctx_per_slot of the shared n_ctx
    hub.loaded = [{"model": "qwen3-8b", "pid": 1, "serving": {"n_ctx": 65536, "n_ctx_per_slot": 32768, "slots": 2}}]
    warm()
    assert cw.window_for("modelmistress/qwen3-8b") == 32768
    mm.forget_context()
    hub.loaded = [{"model": "qwen3-8b", "pid": 1, "serving": {"n_ctx": 32768, "slots": 1}}]
    warm()
    assert cw.window_for("modelmistress/qwen3-8b") == 32768


def test_trained_context_in_the_model_list_is_not_the_window(hub, conf):
    hub.loaded = None
    assert cw.window_for("modelmistress/qwen3-8b") == cw.LOCAL_DEFAULT_WINDOW == 8192


def test_n_ctx_on_the_model_list_is_used(hub, conf):
    hub.loaded = None
    hub.models = [{"id": "qwen3-8b", "metadata": {"context_length": 262144, "n_ctx": 24576}}]
    warm()
    assert cw.window_for("modelmistress/qwen3-8b") == 24576


def test_props_through_the_hub(hub, conf):
    hub.loaded = []
    hub.props = {"default_generation_settings": {"n_ctx": 32768}}
    warm()
    assert cw.window_for("modelmistress/qwen3-8b") == 32768


def test_resolution_order_hub_then_config_then_8192(hub, conf):
    warm()                     # the hub's real figure must be known first (window_for never waits for it: under load it raced)
    conf["context_windows"] = {"qwen3-8b": 12000}
    assert cw.window_for("modelmistress/qwen3-8b") == 12000      # config is the smaller: kept
    conf["context_windows"] = {"qwen3-8b": 100000}
    assert cw.window_for("modelmistress/qwen3-8b") == 16384      # a config figure above the real one is not trusted
    mm.forget_context()
    hub.loaded, hub.models, hub.props = None, [], None           # the hub says nothing
    conf["context_windows"] = {"qwen3-8b": 100000}
    assert cw.window_for("modelmistress/qwen3-8b") == 100000      # then the config
    conf["context_windows"] = {}
    mm.forget_context()
    assert cw.window_for("modelmistress/qwen3-8b") == 8192        # then the conservative default


def test_unknown_local_model_never_gets_128k(monkeypatch, conf):
    monkeypatch.setattr(mm, "find", lambda peer="", refresh=False: None)          # no hub at all
    for name in ("modelmistress/qwen3-coder-30b", "modelmistress/totally-unknown", "modelmistress/llama-3.1-8b"):
        assert cw.window_for(name) == 8192
    assert cw.window_for("totally-unknown", "modelmistress") == 8192
    assert cw.window_for("totally-unknown", "ollama", local=True) == 8192
    assert cw.window_for("totally-unknown") == cw.DEFAULT_WINDOW                   # a hosted unknown model is unchanged
    assert cw.window_for("gpt-4o", "openai") == 128_000


def test_context_lookup_is_cached(hub, conf):
    assert mm.context_length("qwen3-8b") == 16384
    n = len(hub.requests)
    assert mm.context_length("qwen3-8b") == 16384 and len(hub.requests) == n
    hub.loaded = None
    hub.models = []
    miss = mm.context_length("other")
    assert miss is None
    n = len(hub.requests)
    assert mm.context_length("other") is None and len(hub.requests) == n      # misses are cached too, briefly


def test_manage_uses_the_hubs_window(hub, conf):
    transport = SimpleNamespace(router_provider="modelmistress", provider_key="127.0.0.1",
                                prune_tool_results=lambda h, k: (h, 0))
    warm()
    history = [{"role": "user", "content": "x" * 48_000}]                           # ~13.7k tokens: over 70% of 16384
    report = cw.manage(history, transport, "qwen3-8b")
    assert report.window == 16384 and report.needs_summary
    transport.router_provider = "other"
    assert cw.manage(history, transport, "qwen3-8b").window != 16384


def test_model_board_rows_carry_the_counters(monkeypatch):
    from bot.router_brain import learn

    tt.record("fake/m", "text", 3)
    monkeypatch.setattr(learn.store, "rows", lambda sql, params=(): [{"model": "fake/m", "chosen": None, "key": None}]
                        if "model_stats" in sql else [])
    rows = learn.model_board()
    assert rows and rows[0]["tool_calls"]["text"] == 3
