"""The turn's tool pool (bot/agent_runtime/tool_discovery.py): a small local model reaches every tool through find_tools,
and a tool outside the turn's pool is refused even when the model names it."""
from __future__ import annotations

import asyncio
import json

from bot.agent_runtime import small_model, tool_discovery
from bot.agent_runtime.transports.base import NormalizedResponse, ToolCall


class Recorder:
    supports_vision = False
    supports_documents = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent: list[list[str]] = []
        self.prompts: list[str] = []

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": f"tool result {tc.name}: {out}"} for tc, out in results]

    async def send(self, **kw):
        self.sent.append([s["name"] for s in kw["tool_schemas"]])
        self.prompts.append(kw.get("system_prompt") or "")
        return self.responses.pop(0)


def _call(name, args):
    return NormalizedResponse(text="", tool_calls=[ToolCall(f"c-{name}", name, args)],
                              assistant_message={"role": "assistant", "content": name})


def _done(text="done"):
    return NormalizedResponse(text=text, assistant_message={"role": "assistant", "content": text})


def _ask(transport, context=None, workspace=None):
    from bot.backends.native_backend import NativeAgentBackend
    from bot.localchat import chat

    ctx = {"instance_id": chat.ensure_instance(), "chat_id": "tool-discovery-test", **(context or {})}
    if workspace:
        ctx["cwd"] = str(workspace)
    return asyncio.run(NativeAgentBackend(transport, model="local").ask("go", context=ctx, timeout_s=60))


def _small(monkeypatch):
    profile = small_model.SmallProfile(window=32768, reason="test", tools=frozenset({"read_file", "write_file", "grep"}))
    monkeypatch.setattr(small_model, "profile_for", lambda transport, model: profile)


def test_no_turn_means_no_pool_and_nothing_is_refused():
    assert tool_discovery.permitted("anything") is True
    assert tool_discovery.search("memory") == []


def test_search_ranks_the_pool_for_plain_words():
    token = tool_discovery.begin()
    try:
        pool = [{"name": "memory_search", "description": "Search saved memories"},
                {"name": "list_skills", "description": "List the installed skills"},
                {"name": "web_fetch", "description": "Fetch a URL"},
                {"name": "find_tools", "description": "Find and load more tools"}]
        tool_discovery.attach(pool, [])
        assert tool_discovery.search("RAG over my notes")[0]["name"] == "memory_search"     # a synonym reaches it
        assert tool_discovery.search("which skills do I have")[0]["name"] == "list_skills"
        assert all(s["name"] != "find_tools" for s in tool_discovery.search("find more tools"))
        assert tool_discovery.permitted("web_fetch") and not tool_discovery.permitted("admin_db_vacuum")
    finally:
        tool_discovery.end(token)
    assert tool_discovery.permitted("admin_db_vacuum")                                       # the holder is gone


def test_a_small_model_loads_a_tool_with_find_tools_and_then_uses_it(temp_db, monkeypatch):
    _small(monkeypatch)
    t = Recorder([_call("find_tools", {"query": "which skills are installed"}), _call("list_skills", {}), _done()])
    assert _ask(t).text == "done"
    first, second = t.sent[0], t.sent[1]
    assert "find_tools" in first and "list_skills" not in first          # the core set, plus the way to the rest
    assert "list_skills" in second                                       # loaded for the next step
    assert len(t.sent) == 3
    assert tool_discovery.PROMPT_HINT.strip() in t.prompts[0]            # and the prompt says the rest exists


def test_no_hint_when_every_tool_is_sent(temp_db, monkeypatch):
    monkeypatch.setattr(small_model, "profile_for", lambda transport, model: None)
    t = Recorder([_done()])
    _ask(t)
    assert tool_discovery.PROMPT_HINT.strip() not in t.prompts[0]


def test_a_tool_outside_the_turns_pool_is_refused_even_when_named(temp_db, tmp_path):
    t = Recorder([_call("write_file", {"path": "x.txt", "content": "no"}), _done()])
    _ask(t, context={"allowed_tools": frozenset({"read_file"})}, workspace=tmp_path)
    assert t.sent[0] == ["read_file"]                                    # write_file was never offered ...
    assert not (tmp_path / "x.txt").exists()                             # ... and naming it does not run it


def test_find_tools_only_loads_from_the_pool(temp_db, monkeypatch):
    _small(monkeypatch)
    t = Recorder([_call("find_tools", {"query": "write a file"}), _done()])
    _ask(t, context={"allowed_tools": frozenset({"read_file", "grep", "find_tools"})})
    out = json.loads(asyncio.run(tool_discovery._find({"query": "x"})))        # outside a turn: says so, loads nothing
    assert out["tools"] == [] and "No tool pool" in out["note"]
    assert "write_file" not in t.sent[-1]
