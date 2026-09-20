"""Context management (roadmap P3): token estimates calibrated on the provider's counts,
clearing old tool outputs, the moving cache breakpoint, and the loop's use of them."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import db
from bot.agent_runtime import context_window as cw
from bot.agent_runtime.transports import anthropic as anth
from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, ToolCall
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
from bot.backends.native_backend import NativeAgentBackend


@pytest.fixture(autouse=True)
def _clean():
    cw.forget_calibration()
    yield
    cw.forget_calibration()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(cw, "_cfg", lambda: values)
    return values


# ---- windows and calibration ------------------------------------------------------
@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-5", 200_000), ("anthropic/claude-opus-5", 200_000), ("gpt-4.1-mini", 1_000_000),
    ("gpt-4o", 128_000), ("gpt-4", 8_192), ("gemini-2.5-pro", 1_000_000), ("llama-3.1-70b", 128_000),
    ("llama-2-7b", 8_192), ("totally-unknown-model", cw.DEFAULT_WINDOW),
])
def test_window_for_common_families(model, expected, cfg):
    assert cw.window_for(model) == expected


def test_windows_can_be_overridden_per_model_or_prefix(cfg):
    cfg["context_windows"] = {"my-local": 32768, "org/exact-model": "4096", "bad": "lots"}
    assert cw.window_for("my-local-13b") == 32768 and cw.window_for("org/exact-model") == 4096
    assert cw.window_for("bad-model") == cw.DEFAULT_WINDOW


def test_the_estimate_learns_from_the_providers_own_count():
    assert cw.ratio_for("m") == cw.DEFAULT_RATIO
    for _ in range(12):
        cw.observe("m", chars_sent=6000, input_tokens=2000)          # 3.0 chars per token
    assert abs(cw.ratio_for("m") - 3.0) < 0.15
    history = [{"role": "user", "content": "x" * 3000}]
    assert 900 < cw.estimate_tokens(history, "m") < 1100
    assert cw.ratio_for("other-model") == cw.DEFAULT_RATIO


def test_calibration_ignores_tiny_or_missing_samples_and_clamps_wild_ones():
    cw.observe("m", 100, 10)
    cw.observe("m", 5000, None)
    cw.observe("m", 5000, 10)
    assert cw.ratio_for("m") == cw.DEFAULT_RATIO or cw.ratio_for("m") <= cw.MAX_RATIO


def test_images_and_signatures_are_counted_sensibly():
    history = [{"role": "user", "content": [{"type": "image", "source": {"data": "A" * 500_000}}, {"type": "text", "text": "hi"}]},
               {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "S" * 5000}]}]
    assert cw.measure_chars(history) < 10_000


# ---- clearing old tool outputs -------------------------------------------------------
def anthropic_history(n_results, size=4000):
    h = [{"role": "user", "content": "start"}]
    for i in range(n_results):
        h.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}]})
        h.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"result {i} " + "x" * size}]})
    return h


def test_manage_does_nothing_below_the_threshold(cfg):
    h = anthropic_history(3, size=100)
    rep = cw.manage(h, AnthropicTransport(api_key="unused"), "claude-sonnet-5")
    assert rep.history is h and rep.cleared == 0 and not rep.needs_summary


def test_manage_clears_old_anthropic_tool_outputs_and_keeps_recent_ones(cfg):
    cfg["context_windows"] = {"m": 10_000}
    cfg["context"] = {"compact_at": 0.5, "keep_tool_results": 2}
    h = anthropic_history(8)
    original = [dict(e) for e in h]
    rep = cw.manage(h, AnthropicTransport(api_key="unused"), "m")
    assert rep.cleared == 6 and rep.after < rep.before
    results = [e["content"][0]["content"] for e in rep.history if isinstance(e["content"], list) and e["content"][0].get("type") == "tool_result"]
    assert all(r.startswith("[Output cleared") for r in results[:6])
    assert results[6].startswith("result 6") and results[7].startswith("result 7")
    assert "result 0" in results[0]                                   # the note says how it began
    assert h == original, "the stored conversation must not be modified"
    again = cw.manage(rep.history, AnthropicTransport(api_key="unused"), "m")
    assert again.cleared == 0                                         # already-cleared outputs are not cleared twice


def test_manage_clears_old_openai_tool_outputs(cfg):
    cfg["context_windows"] = {"m": 10_000}
    cfg["context"] = {"compact_at": 0.5, "keep_tool_results": 1}
    h = [{"role": "user", "content": "go"}]
    for i in range(5):
        h.append({"role": "assistant", "content": {"content": None, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}})
        h.append({"role": "tool", "content": {"tool_call_id": f"c{i}", "content": f"out {i} " + "y" * 5000}})
    rep = cw.manage(h, OpenAICompatibleTransport("https://x/v1"), "m")
    tools = [e["content"]["content"] for e in rep.history if e["role"] == "tool"]
    assert rep.cleared == 4 and tools[-1].startswith("out 4") and all(t.startswith("[Output cleared") for t in tools[:4])


def test_it_asks_for_a_summary_when_clearing_is_not_enough(cfg):
    cfg["context_windows"] = {"m": 2000}
    cfg["context"] = {"compact_at": 0.5, "keep_tool_results": 8}
    rep = cw.manage(anthropic_history(8), AnthropicTransport(api_key="unused"), "m")
    assert rep.cleared == 0 and rep.needs_summary


def test_a_transport_that_cannot_prune_is_left_alone(cfg):
    cfg["context_windows"] = {"m": 2000}
    rep = cw.manage(anthropic_history(8), ProviderTransport(), "m")
    assert rep.cleared == 0 and rep.needs_summary


def test_compact_at_zero_switches_it_off(cfg):
    cfg["context_windows"] = {"m": 2000}
    cfg["context"] = {"compact_at": 0}
    assert not cw.manage(anthropic_history(8), AnthropicTransport(api_key="unused"), "m").needs_summary


# ---- the moving cache breakpoint --------------------------------------------------------
CC = {"type": "ephemeral"}


def test_the_breakpoint_goes_on_the_last_block_of_the_last_message_without_mutating_history():
    h = [{"role": "user", "content": "hello"}]
    out = anth._with_message_breakpoint(h, CC)
    assert out[-1]["content"] == [{"type": "text", "text": "hello", "cache_control": CC}] and h[0]["content"] == "hello"
    h2 = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]}]
    out2 = anth._with_message_breakpoint(h2, CC)
    assert out2[-1]["content"][-1]["cache_control"] == CC and "cache_control" not in h2[0]["content"][-1]


def test_no_breakpoint_on_thinking_blocks_or_empty_history():
    h = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]}]
    assert anth._with_message_breakpoint(h, CC) is h and anth._with_message_breakpoint([], CC) == []


def test_the_request_carries_the_breakpoint_when_caching_is_on_and_not_otherwise(monkeypatch):
    t = AnthropicTransport(api_key="unused")
    h = [{"role": "user", "content": "hi"}]
    kw = t._build_kwargs(model="m", history=h, tool_schemas=[], max_tokens=5, system_prompt="s", effort=None)
    assert kw["messages"][-1]["content"][-1]["cache_control"] == CC
    monkeypatch.setattr(anth, "_prompt_caching_config", lambda: {"enabled": False})
    kw = t._build_kwargs(model="m", history=h, tool_schemas=[], max_tokens=5, system_prompt="s", effort=None)
    assert kw["messages"] == h


# ---- provider token counts reach the response -------------------------------------------
def test_input_tokens_include_cached_tokens():
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=5, cache_creation_input_tokens=400,
                                                 cache_read_input_tokens=1000))
    assert anth._normalize(resp).input_tokens == 1500


# ---- the stored conversation ----------------------------------------------------------------
def test_a_long_session_returns_its_newest_messages_not_its_oldest(temp_db):
    for i in range(2100):
        db.append_agent_message("long", "user" if i % 2 == 0 else "assistant", f"m{i}")
    got = db.list_agent_messages("long")
    assert len(got) == 2000 and got[0]["content"] == "m100" and got[-1]["content"] == "m2099"
    assert [m["content"] for m in db.list_agent_messages("long", limit=3)] == ["m2097", "m2098", "m2099"]


# ---- in the loop ---------------------------------------------------------------------------
class Filler(ProviderTransport):
    """Asks for a big file over and over, then answers; answers a summary request with a digest."""

    def __init__(self, rounds):
        self.rounds, self.calls, self.sent = rounds, 0, []

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": out} for tc, out in results]}]

    def prune_tool_results(self, history, keep):
        return AnthropicTransport.prune_tool_results(self, history, keep)

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.sent.append((list(history), bool(tool_schemas)))
        if not tool_schemas and "Summarize" in str(history[-1]["content"]):
            return NormalizedResponse(text="digest of earlier work", assistant_message={"role": "assistant", "content": "digest of earlier work"},
                                      input_tokens=10)
        self.calls += 1
        if self.calls <= self.rounds:
            tc = ToolCall(id=f"call{self.calls}", name="read_file", arguments={"path": "big.txt", "offset": self.calls, "limit": 300})
            return NormalizedResponse(text="", tool_calls=[tc], input_tokens=10,
                                      assistant_message={"role": "assistant", "content": [{"type": "tool_use", "id": tc.id, "name": "read_file", "input": tc.arguments}]})
        return NormalizedResponse(text="all done", assistant_message={"role": "assistant", "content": [{"type": "text", "text": "all done"}]}, input_tokens=10)


def test_the_loop_clears_old_outputs_then_summarises_when_the_window_fills(temp_db, tmp_path, cfg, monkeypatch):
    (tmp_path / "big.txt").write_text("\n".join(f"line {i} " + "z" * 60 for i in range(3000)))
    cfg["context_windows"] = {"m": 4000}
    cfg["context"] = {"compact_at": 0.5, "keep_tool_results": 1}
    monkeypatch.setattr(cw, "_cfg", lambda: cfg)
    from bot.agent_runtime import loop_guard

    monkeypatch.setattr(loop_guard, "limits", lambda: loop_guard.Limits(max_iterations=0))
    t = Filler(rounds=7)
    result = asyncio.run(NativeAgentBackend(t, model="m").ask("read the file in pieces", context={"cwd": str(tmp_path), "desktop_session_key": "ctx-loop"}))
    assert result.text == "all done"
    cleared_seen = any("[Output cleared" in str(h) for h, _ in t.sent)
    assert cleared_seen, "old tool outputs should have been cleared from what was sent"
    stored = str(db.list_agent_messages("ctx-loop"))
    assert "[Summary of earlier conversation]" in stored, "the window filled up, so the older conversation should be summarised"
    assert "[Output cleared" not in str(db.list_agent_messages("ctx-loop")[:1])
