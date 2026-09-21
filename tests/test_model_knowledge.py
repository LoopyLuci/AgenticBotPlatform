"""Model knowledge and allowances (roadmap PM): what a model can do, its published limits, what has been
used, and holding calls back before the provider refuses them."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bot import model_catalog, model_pricing
from bot.agent_runtime import context_window as cw
from bot.agent_runtime import prompt, tools, usage_limits
from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport
from bot.backends.base import BackendError
from bot.dashboard.server import build_app

CATALOG = {
    "openrouter": {"id": "openrouter", "api": "https://openrouter.ai/api/v1", "name": "OpenRouter", "models": {
        "qwen/qwen3.8-27b:free": {"id": "qwen/qwen3.8-27b:free", "name": "Qwen3.8 27B (free)", "family": "qwen", "tool_call": True,
                                  "reasoning": True, "structured_output": True, "attachment": True, "open_weights": True,
                                  "release_date": "2026-08-14", "modalities": {"input": ["text", "image"], "output": ["text"]},
                                  "limit": {"context": 262144, "output": 235929}, "cost": {"input": 0, "output": 0}},
        "big/paid": {"id": "big/paid", "name": "Big Paid", "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]},
                     "limit": {"context": 1000000, "output": 64000}, "cost": {"input": 3, "output": 15, "cache_read": 0.3}},
        "small/free:free": {"id": "small/free:free", "name": "Small", "tool_call": False, "modalities": {"input": ["text"], "output": ["text"]},
                            "limit": {"context": 8192, "output": 2048}, "cost": {"input": 0, "output": 0}},
    }},
    "anthropic": {"id": "anthropic", "models": {
        "claude-sonnet-4-6": {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tool_call": True, "knowledge": "2025-08-31",
                              "limit": {"context": 1000000, "output": 128000}, "cost": {"input": 3, "output": 15}}}},
    "groq": {"id": "groq", "api": "https://api.groq.com/openai/v1", "models": {
        "openai/gpt-oss-20b": {"id": "openai/gpt-oss-20b", "name": "GPT OSS 20B", "tool_call": True,
                               "limit": {"context": 131072, "output": 65536}, "cost": {"input": 0.1, "output": 0.5}}}},
}


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    usage_limits._blocked_until.clear()
    usage_limits._reported.clear()
    usage_limits._inflight.clear()
    cw.forget_calibration()
    monkeypatch.setitem(model_pricing._memory_cache, "data", CATALOG)
    yield
    usage_limits._blocked_until.clear()
    usage_limits._reported.clear()


@pytest.fixture
def mcfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(model_catalog, "_cfg", lambda: values)
    monkeypatch.setattr(usage_limits, "_cfg", lambda: values)
    return values


def run(coro):
    return asyncio.run(coro)


# ---- what a model can do -------------------------------------------------------------------------
def test_lookup_reports_capabilities_price_and_where_each_fact_came_from():
    info = model_catalog.lookup("openrouter", "qwen/qwen3.8-27b:free")
    assert info.context == 262144 and info.max_output == 235929 and info.free is True
    assert info.tool_call and info.reasoning and info.vision and info.open_weights
    assert info.release_date == "2026-08-14" and info.sources["context"] == "catalog"
    paid = model_catalog.lookup("openrouter", "big/paid")
    assert paid.free is False and (paid.price_input, paid.price_output, paid.price_cache_read) == (3.0, 15.0, 0.3)
    assert model_catalog.lookup("anthropic", "claude-sonnet-4-6").knowledge == "2025-08-31"


def test_a_provider_is_recognised_by_its_catalog_id_or_its_address(monkeypatch):
    assert model_catalog.catalog_provider_for("api.groq.com") == "groq"
    assert model_catalog.lookup("api.groq.com", "openai/gpt-oss-20b").context == 131072
    monkeypatch.setattr("bot.providers.get_provider", lambda name: {"catalog_id": "openrouter", "base_url": "http://x"} if name == "my-or" else None)
    assert model_catalog.lookup("my-or", "big/paid").context == 1_000_000


def test_an_unknown_model_says_so_instead_of_guessing():
    info = model_catalog.lookup("nowhere", "mystery-model")
    assert info.context is None or info.sources["context"] == "builtin"
    assert any("not in the models.dev catalog" in n for n in info.notes)
    assert not info.limits.known() and any("no published rate limit" in n for n in info.notes)


def test_without_a_downloaded_catalog_the_builtin_table_still_answers(monkeypatch):
    monkeypatch.setitem(model_pricing._memory_cache, "data", None)
    info = model_catalog.lookup("anthropic", "claude-sonnet-5")
    assert info.context == 200_000 and info.sources["context"] == "builtin"


def test_free_is_inferred_from_the_name_only_as_a_last_resort(monkeypatch):
    monkeypatch.setitem(model_pricing._memory_cache, "data", None)
    info = model_catalog.lookup("openrouter", "some/model:free")
    assert info.free is True and "name" in info.sources["price"]


def test_your_overrides_beat_the_catalog(mcfg):
    mcfg["overrides"] = {"openrouter/big/paid": {"context": 32000, "vision": True, "tool_call": False}}
    info = model_catalog.lookup("openrouter", "big/paid")
    assert info.context == 32000 and info.vision and info.tool_call is False and info.sources["override:context"] == "override"


def test_search_filters_and_orders_by_context():
    rows = model_catalog.search(free_only=True)
    assert [r["model"] for r in rows] == ["qwen/qwen3.8-27b:free", "small/free:free"]
    assert [r["model"] for r in model_catalog.search(needs=("tools", "vision"))] == ["qwen/qwen3.8-27b:free"]
    assert [r["model"] for r in model_catalog.search(provider="openrouter", min_context=500_000)] == ["big/paid"]
    assert model_catalog.search(query="gpt-oss")[0]["provider"] == "groq"


# ---- context windows use the catalog ---------------------------------------------------------------
def test_the_context_window_comes_from_the_catalog_but_claude_stays_on_the_safe_side(monkeypatch):
    assert cw.window_for("qwen/qwen3.8-27b:free", "openrouter") == 262144           # the catalog is more exact than the family table
    assert cw.window_for("claude-sonnet-4-6", "anthropic") == 200_000               # catalog says 1M, table says 200k: the safer one
    assert cw.window_for("never-heard-of-it", "openrouter") == cw.DEFAULT_WINDOW
    monkeypatch.setattr(cw, "_cfg", lambda: {"context_windows": {"qwen/qwen3.8-27b:free": 50000}})
    assert cw.window_for("qwen/qwen3.8-27b:free", "openrouter") == 50000            # an explicit override always wins


# ---- published limits ----------------------------------------------------------------------------------
def test_curated_limits_carry_their_source_and_date():
    lim = model_catalog.lookup("openrouter", "qwen/qwen3.8-27b:free").limits
    assert (lim.rpm, lim.rpd, lim.scope, lim.source) == (20, 50, "provider", "curated")
    assert lim.url.startswith("https://openrouter.ai/") and lim.checked
    groq = model_catalog.lookup("groq", "openai/gpt-oss-20b").limits
    assert (groq.rpm, groq.rpd, groq.tpm, groq.tpd) == (30, 1000, 8000, 200000)


def test_a_provider_that_publishes_no_figures_is_not_given_invented_ones():
    lim = model_catalog.lookup("google", "gemini-2.5-flash").limits
    assert not lim.known() and lim.reset == "pacific_midnight" and "AI Studio" in lim.note


def test_your_own_limits_replace_the_published_ones(mcfg):
    mcfg["limits"] = {"openrouter/*:free": {"rpm": 20, "rpd": 1000}}
    lim = model_catalog.lookup("openrouter", "qwen/qwen3.8-27b:free").limits
    assert lim.rpd == 1000 and lim.source == "override"
    mcfg["limits"] = {"openrouter/qwen/qwen3.8-27b:free": {"rpm": 5}}
    assert model_catalog.lookup("openrouter", "qwen/qwen3.8-27b:free").limits.rpm == 5


def test_the_prompt_line_is_short_stable_and_mentions_limits():
    line = model_catalog.prompt_line("openrouter", "qwen/qwen3.8-27b:free")
    assert "context 262,144 tokens" in line and "20/min" in line and "50/day" in line and "model_info" in line
    assert "\n" not in line
    assert prompt.build(None, model_line=line).count("Model: openrouter/qwen") == 1


# ---- reading what providers say ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,seconds", [("1s", 1), ("6m0s", 360), ("20ms", 0.02), ("1h2m3.5s", 3723.5), ("45", 45), ("", None), ("soon", None)])
def test_reset_formats(value, seconds):
    got = usage_limits.parse_wait(value, now=1_000_000.0)
    assert (got is None and seconds is None) or abs(got - seconds) < 1e-6


def test_absolute_reset_times_become_a_wait():
    now = 1_800_000_000.0
    assert abs(usage_limits.parse_wait(str(now + 30), now) - 30) < 1e-6
    assert abs(usage_limits.parse_wait(str(int((now + 30) * 1000)), now) - 30) < 1e-3
    iso = datetime.fromtimestamp(now + 90, timezone.utc).isoformat().replace("+00:00", "Z")
    assert abs(usage_limits.parse_wait(iso, now) - 90) < 1e-3


def test_openai_and_groq_headers():
    parsed = usage_limits.parse_headers({"X-RateLimit-Limit-Requests": "14400", "x-ratelimit-remaining-requests": "14399",
                                         "x-ratelimit-reset-requests": "6s", "x-ratelimit-limit-tokens": "6000",
                                         "x-ratelimit-remaining-tokens": "5990", "x-ratelimit-reset-tokens": "100ms"}, now=0)
    assert parsed["requests"] == {"limit": 14400, "remaining": 14399, "reset_in_s": 6.0}
    assert parsed["tokens"]["remaining"] == 5990


def test_anthropic_and_openrouter_and_retry_after_headers():
    a = usage_limits.parse_headers({"anthropic-ratelimit-requests-limit": "50", "anthropic-ratelimit-requests-remaining": "0",
                                    "anthropic-ratelimit-requests-reset": "1970-01-01T00:01:00Z", "retry-after": "30"}, now=0)
    assert a["requests"]["remaining"] == 0 and a["requests"]["reset_in_s"] == 60 and a["retry_after_s"] == 30
    o = usage_limits.parse_headers({"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "60000"}, now=0)
    assert o["requests"]["limit"] == 20 and o["requests"]["remaining"] == 0
    assert usage_limits.parse_headers({"content-type": "json"}) == {}


# ---- counting and windows ---------------------------------------------------------------------------------------
NOW = 1_800_000_000.0   # 2027-01-15 08:00:00 UTC


def use(provider, model, n, *, tokens=100, ago=0.0, status=200):
    for _ in range(n):
        usage_limits.record(provider, model, tokens=tokens, status=status, now=NOW - ago)


def test_usage_is_counted_per_window_and_survives_a_restart():
    use("groq", "openai/gpt-oss-20b", 3, tokens=1000)
    use("groq", "openai/gpt-oss-20b", 2, tokens=1000, ago=120)
    snap = usage_limits.snapshot("groq", "openai/gpt-oss-20b", now=NOW)
    by = {w["name"]: w for w in snap["windows"]}
    assert (by["requests/min"]["used"], by["requests/min"]["remaining"]) == (3, 27)
    assert (by["requests/day"]["used"], by["tokens/min"]["used"], by["tokens/day"]["used"]) == (5, 3000, 5000)
    assert snap["calls_24h"] == 5 and snap["headroom"] == pytest.approx(min(27 / 30, 995 / 1000, 5000 / 8000, 195000 / 200000))
    assert usage_limits.snapshot("groq", "openai/gpt-oss-20b", now=NOW)["calls_24h"] == 5     # read back from disk


def test_a_shared_allowance_counts_every_model_it_covers():
    use("openrouter", "a/one:free", 2)
    use("openrouter", "b/two:free", 3)
    use("openrouter", "big/paid", 9)                                     # not free: not part of the free allowance
    w = {x["name"]: x for x in usage_limits.snapshot("openrouter", "a/one:free", now=NOW)["windows"]}
    assert w["requests/day"]["used"] == 5 and w["requests/day"]["remaining"] == 45


def test_daily_windows_reset_at_the_providers_midnight(mcfg):
    mcfg["limits"] = {"google/*": {"rpd": 10, "reset": "pacific_midnight"}}
    start, nxt = usage_limits.day_bounds("pacific_midnight", NOW)
    assert nxt - start == 86400 and start <= NOW < nxt
    assert datetime.fromtimestamp(nxt, timezone.utc).strftime("%H:%M") in ("08:00", "07:00")      # midnight Pacific
    use("google", "gemini-x", 4, ago=(NOW - start) + 5)                  # counted yesterday (Pacific): not today
    use("google", "gemini-x", 2)
    w = {x["name"]: x for x in usage_limits.snapshot("google", "gemini-x", now=NOW)["windows"]}["requests/day"]
    assert w["used"] == 2 and w["resets_at"] == nxt


def test_a_rolling_window_frees_up_when_its_oldest_call_ages_out(mcfg):
    mcfg["limits"] = {"p/*": {"rpm": 3}}
    use("p", "m", 1, ago=50)
    use("p", "m", 2, ago=10)
    w = usage_limits.snapshot("p", "m", now=NOW)["windows"][0]
    assert w["remaining"] == 0 and w["resets_in_s"] == pytest.approx(10.0)


# ---- holding calls back --------------------------------------------------------------------------------------------------
def test_an_exhausted_daily_allowance_is_refused_with_the_reset_time(mcfg):
    mcfg["timezone"] = "America/New_York"
    mcfg["limits"] = {"p/*": {"rpd": 5, "reset": "utc_midnight"}}
    use("p", "m", 5)
    with pytest.raises(usage_limits.RateLimited) as err:
        run(usage_limits.before_call("p", "m", now=NOW))
    text = str(err.value)
    assert "used 5 of its 5 requests/day" in text and "EST" in text and "(in 16h00m)" in text
    assert isinstance(err.value, BackendError) and err.value.retry_at > NOW


def test_a_per_minute_limit_waits_when_short_and_refuses_when_long(mcfg, monkeypatch):
    mcfg["limits"] = {"p/*": {"rpm": 2}}
    use("p", "m", 2, ago=57)                                            # frees up in 3 s
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(usage_limits.asyncio, "sleep", fake_sleep)
    run(usage_limits.before_call("p", "m", now=NOW))
    assert slept and 2.9 < slept[0] < 3.2
    mcfg["max_wait_s"] = 1
    with pytest.raises(usage_limits.RateLimited):
        run(usage_limits.before_call("p", "m", now=NOW))


def test_enforcement_can_be_switched_off_and_unknown_limits_never_block(mcfg):
    mcfg["limits"] = {"p/*": {"rpd": 1}}
    use("p", "m", 5)
    mcfg["enforce"] = False
    run(usage_limits.before_call("p", "m", now=NOW))
    mcfg["enforce"] = True
    use("nolimits", "m", 500)
    run(usage_limits.before_call("nolimits", "m", now=NOW))
    assert usage_limits.headroom("nolimits", "m") is None


def test_a_429_makes_us_wait_as_told():
    until = usage_limits.note_rate_limited("p", "m", 120, now=NOW)
    assert until == NOW + 120
    with pytest.raises(usage_limits.RateLimited) as err:
        run(usage_limits.before_call("p", "m", now=NOW + 1))
    assert "answered 429" in str(err.value)
    run(usage_limits.before_call("p", "m", now=NOW + 121))


def test_provider_reported_exhaustion_blocks_until_its_reset(mcfg):
    usage_limits.observe_headers("p", "m", {"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "0",
                                            "x-ratelimit-reset-requests": "300s"}, now=NOW)
    with pytest.raises(usage_limits.RateLimited) as err:
        run(usage_limits.before_call("p", "m", now=NOW + 1))
    assert "no requests left" in str(err.value)
    assert usage_limits.headroom("p", "m") == 0.0 or usage_limits.snapshot("p", "m", now=NOW + 1)["headroom"] == 0.0


def test_concurrency_is_capped(mcfg):
    mcfg["limits"] = {"p/*": {"concurrent": 1}}
    usage_limits._inflight[("p", "m")] = 1
    mcfg["max_wait_s"] = 0
    with pytest.raises(usage_limits.RateLimited):
        run(usage_limits.before_call("p", "m", now=NOW))


# ---- every transport is covered ----------------------------------------------------------------------------------------------
class FakeTransport(ProviderTransport):
    catalog_id = "fakeprov"

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.calls += 1
        return await self.behaviour(self)


def call(t, model="m"):
    return t.send(model=model, history=[{"role": "user", "content": "hi"}], tool_schemas=[], max_tokens=10, timeout_s=5)


def test_a_transports_calls_are_counted_with_their_tokens():
    async def ok(t):
        return NormalizedResponse(text="hi", tokens=321, input_tokens=300)

    t = FakeTransport(ok)
    run(call(t))
    row = usage_limits.report(1)[0]
    assert (row["provider"], row["model"], row["calls"], row["tokens"]) == ("fakeprov", "m", 1, 321)


def test_a_429_is_recorded_and_the_next_call_is_held_back(mcfg):
    async def limited(t):
        t.last_status, t.rate_headers = 429, {"retry-after": "90"}
        raise BackendError("openai-compatible transport (x) returned 429: slow down")

    t = FakeTransport(limited)
    with pytest.raises(BackendError):
        run(call(t))
    assert usage_limits.report(1)[0]["rate_limited"] == 1
    with pytest.raises(usage_limits.RateLimited):
        run(call(t))
    assert t.calls == 1, "the second call must not reach the provider"


def test_the_provider_is_found_from_the_error_text_when_no_status_was_captured():
    async def boom(t):
        raise BackendError("anthropic transport error: Error code: 429 - rate_limit_error")

    t = FakeTransport(boom)
    with pytest.raises(BackendError):
        run(call(t))
    assert usage_limits.report(1)[0]["rate_limited"] == 1


def test_a_streaming_call_that_falls_back_to_send_is_counted_once():
    async def ok(t):
        return NormalizedResponse(text="hi", tokens=10)

    t = FakeTransport(ok)

    async def on_event(e):
        pass

    run(t.send_stream(on_event=on_event, model="m", history=[], tool_schemas=[], max_tokens=1, timeout_s=1))
    assert usage_limits.report(1)[0]["calls"] == 1


def test_real_transports_report_the_provider_they_talk_to():
    from bot.agent_runtime.transports.anthropic import AnthropicTransport
    from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport

    assert AnthropicTransport(api_key="unused").provider_key == "anthropic"
    assert OpenAICompatibleTransport("https://api.groq.com/openai/v1", api_key="unused").provider_key == "api.groq.com"
    assert OpenAICompatibleTransport("http://x/v1", catalog_id="openrouter").provider_key == "openrouter"


def test_capture_keeps_only_rate_limit_headers():
    class R:
        status_code = 429
        headers = {"X-RateLimit-Remaining": "0", "Set-Cookie": "secret", "Retry-After": "5", "content-type": "json"}

    t = FakeTransport(None)
    t.capture(R())
    assert t.last_status == 429 and t.rate_headers == {"x-ratelimit-remaining": "0", "retry-after": "5"}
    t.capture(None)                                                     # a failed request with no response is fine


# ---- the agent can ask ---------------------------------------------------------------------------------------------------------------
def test_model_info_tool_answers_for_the_running_model_and_others():
    token = usage_limits.current_model.set(("openrouter", "qwen/qwen3.8-27b:free"))
    try:
        use("openrouter", "qwen/qwen3.8-27b:free", 4)
        mine = json.loads(run(tools.execute_tool("model_info", {}, workspace=None, instance_id=1)))
        assert mine["context"] == 262144 and mine["quota"]["windows"][0]["of"] == 20
        other = json.loads(run(tools.execute_tool("model_info", {"model": "groq/openai/gpt-oss-20b"}, workspace=None, instance_id=1)))
        assert other["limits"]["tpd"] == 200000 and other["quota"]["used_last_24h"]["calls"] == 0
        with pytest.raises(Exception, match="provider/model"):
            run(tools.execute_tool("model_info", {"model": "nonsense"}, workspace=None, instance_id=1))
    finally:
        usage_limits.current_model.reset(token)
    assert not tools.is_dangerous("model_info") and not tools.is_dangerous("find_models")


def test_find_models_puts_used_up_models_last():
    usage_limits.note_rate_limited("openrouter", "qwen/qwen3.8-27b:free", 600)
    rows = json.loads(run(tools.execute_tool("find_models", {"free_only": True}, workspace=None, instance_id=1)))
    assert [r["model"] for r in rows] == ["small/free:free", "qwen/qwen3.8-27b:free"]
    assert rows[1]["headroom"] == 0.0


def test_read_only_agents_may_use_the_model_tools():
    from bot.agent_runtime import agent_defs

    assert {"model_info", "find_models"} <= set(agent_defs.READ_TOOLS)


# ---- surfaces ------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


H = {"X-Dashboard-Token": "test-token"}


def test_api_reads_and_the_token_gate_on_changes(client):
    use("groq", "openai/gpt-oss-20b", 2, ago=1000)
    r = client.get("/api/models/info", params={"provider": "groq", "model": "openai/gpt-oss-20b"}, headers=H).json()
    assert r["context"] == 131072 and r["limits"]["rpm"] == 30 and r["quota"]["used_last_24h"]["calls"] == 2
    assert client.get("/api/models/usage", headers=H).json()["models"][0]["model"] == "openai/gpt-oss-20b"
    found = client.get("/api/models/find", params={"free_only": True, "needs": "vision"}, headers=H).json()["models"]
    assert [m["model"] for m in found] == ["qwen/qwen3.8-27b:free"]
    assert client.put("/api/models/limits", json={"key": "x/y", "rpm": 1}).status_code == 401
    assert client.put("/api/models/limits", json={"key": "x/y", "rpm": 1}, headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_api_saves_and_clears_your_limits(client, monkeypatch):
    saved = {}
    from bot.config import config

    monkeypatch.setattr(config, "set_value", lambda path, value, actor="x": saved.__setitem__(tuple(path), value))
    r = client.put("/api/models/limits", json={"key": "openrouter/*:free", "rpd": 1000}, headers=H)
    assert r.status_code == 200 and saved[("native_agent", "models", "limits", "openrouter/*:free")] == {"rpd": 1000}
    assert client.put("/api/models/limits", json={"key": "x", "reset": "sometime", "rpm": 1}, headers=H).status_code == 400
    assert client.put("/api/models/limits", json={"key": "x", "rpm": -1}, headers=H).status_code == 400
    assert client.put("/api/models/limits", json={"key": "x"}, headers=H).status_code == 400
    assert client.delete("/api/models/limits", params={"key": "nothing"}, headers=H).status_code == 404


def test_the_slash_command_shows_the_model_and_its_allowance():
    from bot import commands

    use("openrouter", "qwen/qwen3.8-27b:free", 3, ago=2)
    ctx = commands.CmdContext(instance_id=None, instance_name="t", user_id=1, chat_id=1, actor="test")
    out = run(commands.cmd_modelinfo(ctx, ["openrouter/qwen/qwen3.8-27b:free"]))
    assert "context 262,144" in out and "requests/day" in out and "20 requests/min" in out
    assert "Usage:" in run(commands.cmd_modelinfo(ctx, []))
    assert "openrouter/qwen/qwen3.8-27b:free: 3 calls" in run(commands.cmd_modelinfo(ctx, ["usage"]))
    from bot import slash_commands

    assert slash_commands.resolve_command("limits") == "modelinfo"


def test_times_are_shown_in_the_configured_zone(mcfg):
    mcfg["timezone"] = "America/New_York"
    assert usage_limits.show_time(NOW).endswith("EST") and usage_limits.show_time(NOW).startswith("2027-01-15 03:00")
    assert usage_limits.show_span(45) == "45s" and usage_limits.show_span(600) == "10m" and usage_limits.show_span(7300) == "2h01m"


# ---- what uses the allowance ---------------------------------------------------------------------------------------------------------------
def test_an_exhausted_primary_model_fails_over_without_a_wasted_call(mcfg, temp_db, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from bot import agent_settings, bot_instances, providers
    from bot.backends.api_backend import ApiBackend

    calls = []

    class Messages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("the primary must not be called")

    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda api_key: SimpleNamespace(messages=Messages()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")

    class Resp:
        status_code, headers = 200, {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "answered by the fallback"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def post(self, url, json=None, headers=None):
            return Resp()

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: Client())
    providers.set_provider("fallback_provider", base_url="https://fallback.example/v1", api_key="unused")
    instance_id = bot_instances.create_instance(name="w", platform="telegram", backend="api",
                                                credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    agent_settings.set_settings(instance_id, fallback_provider="fallback_provider", fallback_model="fb")
    mcfg["limits"] = {"anthropic/*": {"rpd": 1}}
    use("anthropic", "claude-sonnet-5", 1, ago=0)
    usage_limits.record("anthropic", "claude-sonnet-5", tokens=1, now=time.time())          # today's one request is spent

    result = run(ApiBackend().ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))
    assert result.text == "answered by the fallback" and calls == []


def test_a_free_model_that_is_used_up_is_passed_over_when_picking_one(monkeypatch, mcfg):
    from bot.support_bot import synthetic_gen

    async def fake(*a, **kw):
        return {"alpha": [{"id": "a-one", "free": True}, {"id": "a-two", "free": True}]}, "live"

    monkeypatch.setattr("bot.models.custom_models_with_pricing", fake)
    monkeypatch.setattr("bot.providers.list_providers", lambda: {"alpha": {}})
    assert run(synthetic_gen._free_provider_models({})) == [("alpha", "a-one")]
    usage_limits.note_rate_limited("alpha", "a-one", 600)
    assert run(synthetic_gen._free_provider_models({})) == [("alpha", "a-two")]
    usage_limits.note_rate_limited("alpha", "a-two", 600)
    assert run(synthetic_gen._free_provider_models({})) == []
