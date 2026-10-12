"""The dollar budget for one turn (bot/agent_runtime/turn_budget.py), against fake transports that report usage."""
from __future__ import annotations

import asyncio

import pytest

from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say
from bot.agent_runtime import toolspec, trace, turn_budget
from bot.agent_runtime.transports.base import NormalizedResponse
from bot.backends.native_backend import NativeAgentBackend

# Every fake response is 1000 input + 500 output tokens.
IN, OUT = 1000, 500


class Priced(ScriptedTransport):
    """A scripted transport that reports usage like a real one, and says which provider it is."""

    def __init__(self, script, catalog_id="paidco", base_url=None):
        super().__init__(script)
        self.catalog_id = catalog_id
        if base_url:
            self.base_url = base_url

    async def send(self, **kw):
        r = await super().send(**kw)
        return NormalizedResponse(text=r.text, tool_calls=r.tool_calls, assistant_message=r.assistant_message,
                                  tokens=IN + OUT, input_tokens=IN)


def configure(monkeypatch, **budget):
    from bot import config as cfg

    monkeypatch.setattr(cfg.config, "_data", {"native_agent": {"budget": budget}} if budget else {})


# $2 per call at these prices: 1000 * $1000/M + 500 * $2000/M
PRICES = {"paidco/m": {"input": 1000, "output": 2000}}


def listing(n):
    return [Call("list_dir", {"path": str(i)}) for i in range(n)]


@pytest.fixture(autouse=True)
def _trace_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "traces.db"))
    monkeypatch.setattr(trace, "_stores", {})


def run(backend, tmp_path, **ctx):
    return asyncio.run(backend.ask("go", context={"cwd": str(tmp_path), **ctx}))


def notes(result):
    return [e["data"]["message"] for e in trace.run_events(result.raw["trace_run"]) if e["kind"] == "note"]


def test_the_turn_stops_at_the_limit_like_the_token_budget(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=3, prices=PRICES)
    t = Priced(listing(2) + [Say("Listed two folders; the rest remains.")])
    result = run(NativeAgentBackend(t, model="m"), tmp_path)
    assert result.raw["stopped"].startswith("it reached the budget of $3 for one turn")
    assert "Listed two folders" in result.text and "Say \"continue\"" in result.text
    assert t.calls == 3                                  # two real calls ($4 spent) + the wrap-up
    assert result.raw["cost_usd"] == pytest.approx(6.0)  # the wrap-up call is counted too


def test_off_by_default_changes_nothing(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch)
    t = Priced(listing(5) + [Say("done")])
    result = run(NativeAgentBackend(t, model="m"), tmp_path)
    assert result.text == "done" and "stopped" not in result.raw and "cost_usd" not in result.raw
    assert t.calls == 6
    # the trace still records what is known (here: nothing, since no price is configured)
    assert trace.summarize(result.raw["trace_run"])["usd"] is None


def test_the_trace_records_usd_per_call_and_per_run(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, prices=PRICES)               # a price table but no limit
    result = run(NativeAgentBackend(Priced(listing(1) + [Say("ok")]), model="m"), tmp_path)
    summary = trace.summarize(result.raw["trace_run"])
    assert summary["usd"] == pytest.approx(4.0)
    ends = [e["data"] for e in trace.run_events(result.raw["trace_run"]) if e["kind"] == "agent.run.end"]
    assert ends[0]["usd"] == pytest.approx(4.0)


def test_unknown_price_is_allowed_by_default_with_one_note(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=1)
    t = Priced(listing(3) + [Say("done")], catalog_id="mystery")
    result = run(NativeAgentBackend(t, model="who-knows"), tmp_path)
    assert result.text == "done" and "stopped" not in result.raw
    said = [n for n in notes(result) if "no price is known" in n]
    assert len(said) == 1 and "who-knows" in said[0]


def test_unknown_price_can_stop_the_turn(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=1, unknown_price="stop")
    t = Priced(listing(5) + [Say("Stopped early.")], catalog_id="mystery")
    result = run(NativeAgentBackend(t, model="who-knows"), tmp_path)
    assert "price is not known" in result.raw["stopped"]
    assert t.calls == 2                                  # one real call + the wrap-up


def test_a_local_engine_is_free(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=0.0001, unknown_price="stop")
    t = Priced(listing(4) + [Say("done")], catalog_id="ollama")
    result = run(NativeAgentBackend(t, model="qwen"), tmp_path)
    assert result.text == "done" and "stopped" not in result.raw
    assert result.raw["cost_usd"] == 0


def test_a_private_address_alone_is_not_free(temp_db, tmp_path, monkeypatch):
    # A router or proxy on the owner's network may forward to a paid API: unknown, not free.
    configure(monkeypatch, max_usd_per_turn=0.0001, unknown_price="stop")
    t = Priced(listing(4) + [Say("done")], catalog_id=None, base_url="http://192.168.1.20:8080/v1")
    result = run(NativeAgentBackend(t, model="qwen"), tmp_path)
    assert "stopped" in result.raw


def test_a_proxy_marked_free_in_prices_is_free(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=0.0001, unknown_price="stop", prices={"qwen": {"free": True}})
    t = Priced(listing(4) + [Say("done")], catalog_id=None, base_url="http://192.168.1.20:8080/v1")
    result = run(NativeAgentBackend(t, model="qwen"), tmp_path)
    assert result.text == "done" and result.raw["cost_usd"] == 0


def test_a_sub_agent_spends_from_the_turns_budget(temp_db, tmp_path, monkeypatch):
    configure(monkeypatch, max_usd_per_turn=3, prices=PRICES)

    async def handler(inp, **kw):
        child = NativeAgentBackend(Priced([Say("child done")]), model="m", session_prefix="ephemeral")
        return (await child.ask("sub-task", context={"cwd": str(tmp_path)})).text

    toolspec.register({"name": "run_child", "description": "child", "input_schema": {"type": "object", "properties": {}}},
                      toolspec.ToolSpec("run_child", "read", read_only=True, concurrency_safe=True, origin="registered"), handler)
    try:
        t = Priced([Call("run_child", {}), Call("list_dir", {"path": "."}), Say("parent summary")])
        result = run(NativeAgentBackend(t, model="m"), tmp_path)
    finally:
        toolspec.unregister("run_child")
    # the parent's first call is $2 and the child's one call is $2: over $3 before the parent's second call
    assert result.raw["stopped"].startswith("it reached the budget of $3")
    assert t.calls == 2


def test_prices_come_from_the_local_catalog_without_the_network(monkeypatch):
    from bot import model_catalog

    raw = {"acme": {"models": {"big": {"cost": {"input": 3, "output": 15, "cache_read": 0.3}},
                               "gratis": {"cost": {"input": 0, "output": 0}}}}}
    monkeypatch.setattr(model_catalog, "_raw", lambda: raw)
    configure(monkeypatch)

    class T:
        provider_key = "acme"

    price = turn_budget.price_for(T(), "big")
    assert (price.input, price.output, price.cache_read, price.source) == (3, 15, 0.3, "catalog")
    assert turn_budget.price_for(T(), "gratis").input == 0
    assert turn_budget.price_for(T(), "missing") is None
    # as the Anthropic transport reports it: tokens exclude the cached part, input_tokens include it
    resp = NormalizedResponse(text="", tokens=1100, input_tokens=1000, cache_read_tokens=400, assistant_message={})
    # 600 plain input * $3 + 500 output * $15 + 400 cached * $0.3, per million
    assert turn_budget.cost_of(resp, price) == pytest.approx((600 * 3 + 500 * 15 + 400 * 0.3) / 1e6)


def test_a_config_price_beats_the_catalog_and_patterns_match(monkeypatch):
    configure(monkeypatch, prices={"acme/*": {"input": 1, "output": 2}, "acme/free-one": {"free": True}})

    class T:
        provider_key = "acme"

    assert turn_budget.price_for(T(), "anything").output == 2
    assert turn_budget.price_for(T(), "free-one").input == 0
