"""The capacity governor (bot/capacity): classifying provider failures, the state machine and its persistence, what the
state means for routing, handoff packs, and the API / CLI / MCP surfaces."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from bot import capacity, model_router
from bot.capacity import classify, governor, handoff, routing
from bot.capacity.classify import RATE_LIMIT, USAGE_CAP
from bot.model_pricing import _memory_cache as catalog_cache

NOW = 1_800_000_000.0   # a fixed clock for the parsers and the state machine


@pytest.fixture(autouse=True)
def clean():
    governor._last_seen.clear()
    yield
    governor._last_seen.clear()


# ---- classification: real-looking payloads ------------------------------------------------------------------------------
ANTHROPIC_RATE = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message":
                             "This request would exceed your organization's rate limit of 30,000 input tokens per minute. Please try again later."}})
ANTHROPIC_CREDIT = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                               "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}})
ANTHROPIC_REGAIN = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                               "You have reached your specified API usage limits. You will regain access on 2027-01-01 at 00:00 UTC."}})
OPENAI_QUOTA = json.dumps({"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                                     "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}})
OPENAI_TPM = json.dumps({"error": {"message": "Rate limit reached for gpt-4o in organization org-abc on tokens per min (TPM): Limit 30000, "
                                              "Used 29000, Requested 4000. Please try again in 6.2s.", "type": "tokens", "code": "rate_limit_exceeded"}})
OPENAI_RPD = json.dumps({"error": {"message": "Rate limit reached for gpt-4o in organization org-abc on requests per day (RPD): Limit 200, "
                                              "Used 200, Requested 1. Please try again in 7m12s.", "type": "requests", "code": "rate_limit_exceeded"}})
OPENROUTER_CREDITS = json.dumps({"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}})
OPENROUTER_DAILY = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
                                         "code": 429, "metadata": {"headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                                                                "X-RateLimit-Reset": str(int((NOW + 7200) * 1000))}}}})
GEMINI_MINUTE = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
                                      "You exceeded your current quota. Quota exceeded for metric generate_content_requests PerMinutePerProject. "
                                      "Please retry in 34s."}})
GEMINI_DAILY = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
                                     "You exceeded your current quota, please check your plan and billing details."}})


@pytest.mark.parametrize("status,text,headers,kind", [
    (429, ANTHROPIC_RATE, {"retry-after": "12"}, RATE_LIMIT),
    (400, ANTHROPIC_CREDIT, None, USAGE_CAP),
    (400, ANTHROPIC_REGAIN, None, USAGE_CAP),
    (429, OPENAI_QUOTA, None, USAGE_CAP),
    (429, OPENAI_TPM, None, RATE_LIMIT),
    (429, OPENAI_RPD, None, USAGE_CAP),
    (402, OPENROUTER_CREDITS, None, USAGE_CAP),
    (429, OPENROUTER_DAILY, None, USAGE_CAP),
    (429, GEMINI_MINUTE, None, RATE_LIMIT),
    (429, GEMINI_DAILY, None, USAGE_CAP),
    (429, "slow down", None, RATE_LIMIT),
    (None, "cli exited 1: Claude AI usage limit reached|1800003600", None, USAGE_CAP),
    (None, "cli exited 1: 5-hour limit reached ∙ resets 3pm", None, USAGE_CAP),
    (None, "You've hit your limit · resets 7pm (Europe/Berlin)", None, USAGE_CAP),
    (None, "Weekly limit reached ∙ resets Oct 20, 9am", None, USAGE_CAP),
    (200, "all fine", {"anthropic-ratelimit-unified-status": "rejected"}, USAGE_CAP),
    (400, "overage request rejected: extra usage is disabled", None, USAGE_CAP),
])
def test_classification_table(status, text, headers, kind):
    got = classify.classify(status, text, headers, now=NOW)
    assert got is not None and got.kind == kind, got


@pytest.mark.parametrize("status,text", [
    (529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
    (500, "internal server error"),
    (401, '{"error":{"message":"Incorrect API key provided","type":"invalid_request_error","code":"invalid_api_key"}}'),
    (400, "This model's maximum context length is 32768 tokens"),
    (503, "upstream overloaded"),
    (None, "cli exited 1: command not found"),
    (None, ""),
])
def test_things_that_are_not_capacity_are_ignored(status, text):
    assert classify.classify(status, text, now=NOW) is None


def test_reset_times_are_read_from_the_message():
    assert classify.classify(None, "Claude AI usage limit reached|1800003600", now=NOW).reset_at == 1800003600
    s = classify.classify(400, ANTHROPIC_REGAIN, now=1_700_000_000.0)
    assert s.reset_at == pytest.approx(1798761600.0)                        # 2027-01-01T00:00Z
    assert classify.classify(429, OPENROUTER_DAILY, now=NOW).reset_at == pytest.approx(NOW + 7200)
    assert classify.classify(429, OPENAI_RPD, now=NOW).reset_at == pytest.approx(NOW + 432)   # "try again in 7m12s"
    clock = classify.classify(None, "5-hour limit reached ∙ resets 3pm (UTC)", now=NOW)
    assert NOW < clock.reset_at <= NOW + 86400
    assert classify.classify(None, "limit reached", now=NOW).reset_at is None


def test_retry_after_comes_from_the_header_or_the_body():
    assert classify.classify(429, ANTHROPIC_RATE, {"retry-after": "12"}, now=NOW).retry_after_s == 12
    assert classify.classify(429, OPENAI_TPM, now=NOW).retry_after_s == pytest.approx(6.2)
    assert classify.classify(429, GEMINI_MINUTE, now=NOW).retry_after_s == pytest.approx(34)


# ---- the state machine ---------------------------------------------------------------------------------------------------
def cap(reset=None, retry=None):
    return classify.Signal(USAGE_CAP, reset_at=reset, retry_after_s=retry, reason="usage limit reached")


def rl(retry=None):
    return classify.Signal(RATE_LIMIT, retry_after_s=retry, reason="rate limited")


def kinds():
    return [h["kind"] for h in reversed(governor.history())]


def test_the_default_state_is_cloud_led_and_automatic():
    s = governor.get_state(NOW)
    assert s["state"] == "cloud-led" and s["mode"] == "auto" and s["override"] is None


def test_a_usage_cap_with_a_reset_time_means_local_only_until_then():
    moved = governor.observe(cap(reset=NOW + 7200), provider="anthropic", model="x", now=NOW)
    assert moved["state"] == "local-only"
    assert governor.get_state(NOW + 7000)["state"] == "local-only"
    after = governor.get_state(NOW + 7201)
    assert after["state"] == "cloud-led" and after["auto"]["reason"] == "the provider's reset time passed"
    assert kinds() == ["usage_cap", "auto"]


def test_a_cap_without_a_reset_holds_then_probes_in_hybrid_and_a_working_call_recovers():
    governor.observe(cap(), provider="anthropic", now=NOW)
    assert governor.get_state(NOW + 3000)["state"] == "local-only"
    probe = governor.get_state(NOW + 3601)
    assert probe["state"] == "hybrid" and probe["auto"]["probation"]
    governor.observe_success("anthropic", "x", now=NOW + 3700)
    done = governor.get_state(NOW + 3700)
    assert done["state"] == "cloud-led" and done["auto"]["strikes"] == 0


def test_a_cap_during_probation_goes_back_to_local_only_with_a_longer_hold():
    governor.observe(cap(), provider="a", now=NOW)
    governor.get_state(NOW + 3601)                                   # probation
    governor.observe(cap(), provider="a", now=NOW + 3700)
    s = governor.get_state(NOW + 3700)
    assert s["state"] == "local-only" and s["auto"]["until"] == pytest.approx(NOW + 3700 + 7200)


def test_the_hold_is_capped():
    for i in range(8):
        governor.observe(cap(), provider=f"a{i}", now=NOW + i * 100000)
    assert governor.get_state(NOW + 7 * 100000)["auto"]["until"] - (NOW + 7 * 100000) <= 21600


def test_a_few_short_rate_limits_stay_cloud_led_and_a_burst_turns_hybrid_on():
    governor.observe(rl(5), provider="o", now=NOW)
    governor.observe(rl(5), provider="o2", now=NOW + 10)
    assert governor.get_state(NOW + 11)["state"] == "cloud-led" and governor.get_state(NOW + 11)["auto"]["recent_rate_limits"] == 2
    governor.observe(rl(5), provider="o3", now=NOW + 20)
    assert governor.get_state(NOW + 21)["state"] == "hybrid"
    assert governor.get_state(NOW + 21 + 601)["state"] == "cloud-led"


def test_rate_limits_far_apart_do_not_add_up():
    for i in range(5):
        governor.observe(rl(5), provider=f"p{i}", now=NOW + i * 400)
    assert governor.get_state(NOW + 2000)["state"] == "cloud-led"


def test_a_long_retry_after_counts_as_a_usage_cap():
    governor.observe(rl(retry=7200), provider="o", now=NOW)
    s = governor.get_state(NOW + 1)
    assert s["state"] == "local-only" and s["auto"]["reset_known"] and s["auto"]["until"] == pytest.approx(NOW + 7200)


def test_the_same_failure_seen_by_two_layers_counts_once():
    for _ in range(5):
        governor.observe(rl(5), provider="same", now=NOW)
    assert governor.get_state(NOW)["auto"]["recent_rate_limits"] == 1


def test_a_manual_override_wins_expires_and_leaves_the_automatic_state_alone():
    governor.observe(cap(reset=NOW + 99999), provider="a", now=NOW)
    s = governor.set_override("cloud-led", actor="owner", reason="I topped up", ttl_s=600, now=NOW + 1)
    assert s["state"] == "cloud-led" and s["mode"] == "manual" and s["auto"]["state"] == "local-only"
    assert governor.get_state(NOW + 500)["state"] == "cloud-led"
    back = governor.get_state(NOW + 700)
    assert back["state"] == "local-only" and back["mode"] == "auto" and back["override"] is None
    assert "override_expired" in kinds()


def test_clearing_the_override_and_bad_input():
    governor.set_override("local-only", actor="owner", reason="testing", now=NOW)
    assert governor.get_state(NOW + 10**6)["state"] == "local-only"          # no expiry: stays until cleared
    assert governor.clear_override(actor="owner", now=NOW + 5)["state"] == "cloud-led"
    assert governor.set_override("auto", now=NOW + 6)["mode"] == "auto"
    with pytest.raises(ValueError):
        governor.set_override("sideways", now=NOW)
    with pytest.raises(ValueError):
        governor.set_override("hybrid", expires_at=NOW - 5, now=NOW)
    first = governor.history()[-1]
    assert first["actor"] == "owner" and first["reason"] == "testing" and first["to"] == "local-only"


def test_the_state_survives_a_restart_and_the_history_keeps_who_and_why():
    governor.observe(cap(reset=NOW + 5000), provider="anthropic", model="m", now=NOW)
    governor.set_override("hybrid", actor="mcp", reason="demo", now=NOW + 1)
    governor._last_seen.clear()          # a "restart": only the database is left
    s = governor.get_state(NOW + 2)
    assert s["state"] == "hybrid" and s["override"]["actor"] == "mcp" and s["auto"]["state"] == "local-only"
    rows = governor.history()
    assert [r["actor"] for r in reversed(rows)] == ["auto", "mcp"] and "anthropic/m" in rows[-1]["reason"]
    assert rows[-1]["detail"]["kind"] == USAGE_CAP


def test_a_disabled_governor_ignores_signals_and_reports_cloud_led(monkeypatch):
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"enabled": False})
    assert governor.observe(cap(), provider="a", now=NOW) is None
    assert governor.effective_state() == "cloud-led"


def test_a_config_pin_applies_and_an_override_beats_it(monkeypatch):
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"mode": "local-only"})
    s = governor.get_state(NOW)
    assert s["state"] == "local-only" and s["mode"] == "config"
    assert governor.set_override("hybrid", now=NOW)["state"] == "hybrid"


def test_the_entry_points_never_raise_and_ignore_local_providers():
    assert capacity.observe_failure("modelmistress", "q", 429, "slow down", local=True, now=NOW) is None
    assert capacity.observe_failure("x", "y", 429, object(), now=NOW) is None
    moved = capacity.observe_text("claude", "cli exited 1: Claude AI usage limit reached|1800003600", now=NOW)
    assert moved["state"] == "local-only"
    capacity.observe_ok("x", "y")


# ---- routing -------------------------------------------------------------------------------------------------------------
CATALOG = {
    "p": {"id": "p", "models": {
        "big": {"id": "big", "tool_call": True, "reasoning": True, "limit": {"context": 200000, "output": 8000}, "cost": {"input": 3, "output": 15}}}},
    "modelmistress": {"id": "modelmistress", "models": {
        "qwen": {"id": "qwen", "tool_call": True, "limit": {"context": 32000, "output": 4000}, "cost": {"input": 0, "output": 0}}}},
    "ollama": {"id": "ollama", "models": {
        "llama": {"id": "llama", "tool_call": True, "limit": {"context": 32000, "output": 4000}, "cost": {"input": 0, "output": 0}}}},
}
HARD = "analyze the trade-offs of this architecture and design a migration strategy"
EASY = "what is 2 + 2?"


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setitem(catalog_cache, "data", CATALOG)
    from bot.router_brain import learn, policy

    policy.invalidate()
    learn._model_cache.clear()
    monkeypatch.setitem(policy.DEFAULT["learning"], "explore", 0.0)
    yield
    policy.invalidate()


def picks(task, cands=("p/big", "modelmistress/qwen")):
    return [r.model for r in model_router.recommend(task, candidates=list(cands))[1]]


def test_cloud_led_changes_nothing(catalog):
    assert set(picks(EASY)) == {"p/big", "modelmistress/qwen"}


def test_local_only_ranks_only_local_models_and_says_why(catalog):
    governor.set_override("local-only", now=time.time())
    assert picks(HARD) == ["modelmistress/qwen"]
    _, _, skipped = model_router.recommend(HARD, candidates=["p/big", "modelmistress/qwen"])
    assert any("left out: capacity is local-only" in s for s in skipped)


def test_local_only_never_offers_a_cloud_model(catalog, monkeypatch):
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["p/big", "anthropic/claude-x"])
    governor.set_override("local-only", now=time.time())
    assert picks(EASY, cands=("p/big", "anthropic/claude-x")) == []


def test_local_only_uses_the_local_models_abp_can_see_when_the_list_has_none(catalog, monkeypatch):
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["p/big", "ollama/llama", "modelmistress/qwen"])
    governor.set_override("local-only", now=time.time())
    assert routing.local_candidates() == ["modelmistress/qwen", "ollama/llama"]       # ModelMistress first, Ollama after
    assert set(picks(EASY, cands=("p/big",))) == {"modelmistress/qwen", "ollama/llama"}


def test_hybrid_sends_hard_work_to_the_cloud_and_the_rest_to_local_models(catalog):
    governor.set_override("hybrid", now=time.time())
    assert "p/big" in picks(HARD)
    assert picks(EASY) == ["modelmistress/qwen"]
    assert picks(EASY, cands=("p/big",)) == ["p/big"], "hybrid prefers local but never leaves a task with no model"


def test_hybrid_cloud_classes_are_configurable(catalog, monkeypatch):
    from bot.config import config

    monkeypatch.setitem(config._data, "capacity", {"hybrid": {"cloud_classes": ["trivial"]}})
    governor.set_override("hybrid", now=time.time())
    assert "p/big" in picks(EASY) and picks(HARD) == ["modelmistress/qwen"]


def test_a_sticky_cloud_pick_is_rechosen_in_local_only(catalog):
    assert model_router.reroute_reason("p/big", EASY) is None
    governor.set_override("local-only", now=time.time())
    assert "local-only" in model_router.reroute_reason("p/big", EASY)
    assert model_router.reroute_reason("modelmistress/qwen", EASY) is None
    governor.set_override("hybrid", now=time.time())
    assert model_router.reroute_reason("p/big", EASY) and model_router.reroute_reason("p/big", HARD) is None


def test_route_decide_describes_where_a_task_goes(catalog):
    governor.set_override("hybrid", now=time.time())
    assert routing.decide(HARD)["where"] == "cloud allowed" and routing.decide(EASY)["where"] == "local models only"


def test_locality_checks():
    assert routing.is_local_ref("ollama/llama") and not routing.is_local_ref("anthropic/claude-x")
    assert routing.is_local_host("http://127.0.0.1:11434/v1") and not routing.is_local_host("https://api.openai.com/v1")

    class Local:
        base_url = "http://localhost:8000/v1"

    class Cloud:
        base_url = "https://api.openai.com/v1"

    assert routing.is_local_transport(Local()) and not routing.is_local_transport(Cloud())


# ---- where failures are seen ---------------------------------------------------------------------------------------------
def test_a_failed_api_call_reaches_the_governor_and_a_local_one_does_not():
    from bot.agent_runtime import usage_limits
    from bot.backends.base import BackendError

    class Cloud:
        base_url = "https://api.openai.com/v1"

    class Local:
        base_url = "http://127.0.0.1:8001/v1"

    err = BackendError("openai-compatible transport (https://api.openai.com/v1) returned 429: " + OPENAI_QUOTA)
    usage_limits._capacity_failure(Local(), "127.0.0.1", "m", 429, err, {})
    assert governor.get_state()["state"] == "cloud-led"
    usage_limits._capacity_failure(Cloud(), "api.openai.com", "gpt", 429, err, {})
    assert governor.get_state()["state"] == "local-only" and err.capacity_seen


def test_a_cli_agent_hitting_its_limit_reaches_the_governor_through_the_error_text():
    from bot.backends.base import BackendError

    # bot/router.py passes str(exc) of a failed backend that no API transport reported (capacity_seen not set)
    exc = BackendError("cli exited 1: Claude AI usage limit reached|%d" % (time.time() + 7200))
    assert not getattr(exc, "capacity_seen", False)
    capacity.observe_text("claude_code", str(exc))
    s = governor.get_state()
    assert s["state"] == "local-only" and s["auto"]["reset_known"]


# ---- handoff packs ---------------------------------------------------------------------------------------------------------
def test_a_handoff_pack_round_trips_and_renders_a_resume_prompt():
    pack = handoff.write(handoff.HandoffPack(
        task_id="t-1", goal="fix the failing auth test", plan=["read auth.py", {"step": "patch the check", "done": True}],
        files_touched=["bot/auth.py", "bot/auth.py", "tests/test_auth.py"], diff="--- a/bot/auth.py\n+++ b/bot/auth.py\n@@\n-x\n+y\n",
        test_status={"status": "failing", "command": "pytest tests/test_auth.py", "summary": "1 failed"},
        notes=["the token check is case sensitive"], last_error="AssertionError: 401 != 200", model="local/qwen"), now=NOW)
    got = handoff.read("t-1")
    assert got.to_dict() == pack.to_dict() and got.files_touched == ["bot/auth.py", "tests/test_auth.py"]
    text = handoff.resume_prompt(got)
    for needle in ("fix the failing auth test", "[x] patch the check", "bot/auth.py", "failing", "401 != 200", "case sensitive", "+y", "local/qwen"):
        assert needle in text
    again = handoff.update("t-1", append_notes=["tried lowercasing"], test_status={"status": "passing"}, now=NOW + 5)
    assert again.notes[-1] == "tried lowercasing" and again.test_status["status"] == "passing" and again.created_at == NOW
    assert [p["task_id"] for p in handoff.list_packs()] == ["t-1"]
    assert handoff.delete("t-1") and handoff.read("t-1") is None and not handoff.delete("t-1")


def test_a_pack_is_bounded_validated_and_stays_in_its_folder():
    p = handoff.write(handoff.HandoffPack(task_id="big", goal="g", diff="x" * 100_000, notes=[str(i) for i in range(500)],
                                          test_status={"status": "weird"}))
    assert len(p.diff) <= handoff.MAX_DIFF + 100 and len(p.notes) == handoff.MAX_LIST and p.test_status["status"] == "unknown"
    with pytest.raises(ValueError):
        handoff.write(handoff.HandoffPack(task_id=" "))
    with pytest.raises(ValueError):
        handoff.update("big", bogus=1)
    handoff.write(handoff.HandoffPack(task_id="../../escape"))
    assert all(q.parent == handoff.pack_dir() for q in handoff.pack_dir().glob("*.json"))
    assert handoff.read("../../escape").task_id == "../../escape"


def test_a_pack_travels_on_the_work_board_task(tmp_path):
    from bot.coordination import LocalBoard

    board = LocalBoard(tmp_path / "b.db")
    try:
        task, _ = board.create_task("abp/test", "port the thing", key="k1")
        claimed = board.claim(task["id"], "worker-1", lease_s=60)
        pack = handoff.write(handoff.HandoffPack(task_id=task["id"], goal="port the thing", board="abp/test", notes=["half done"]))
        updated = handoff.attach_to_board(board, pack, "worker-1", claimed["fence"])
        assert updated["state"] == "running" and "handoff pack saved" in updated["note"]
        fresh = handoff.from_board_task(board.get_task(task["id"]))
        assert fresh.goal == "port the thing" and fresh.notes == ["half done"]
        assert handoff.from_board_task({"result": None}) is None
    finally:
        board.close()


# ---- API, CLI, MCP ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    return TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}


def test_the_api(client):
    c, h = client
    assert c.get("/api/capacity/state").status_code == 401
    assert c.post("/api/capacity/override", json={"state": "hybrid"}).status_code == 401
    s = c.get("/api/capacity/state", headers=h).json()
    assert s["state"] == "cloud-led" and s["settings"]["rate_limit_threshold"] == 3
    r = c.post("/api/capacity/override", headers=h, json={"state": "local-only", "reason": "api test", "ttl_s": 3600}).json()
    assert r["state"] == "local-only" and r["override"]["expires_at"] > time.time()
    assert c.post("/api/capacity/override", headers=h, json={"state": "nope"}).status_code == 400
    assert c.get("/api/capacity/route", headers=h, params={"task": EASY}).json()["where"] == "local models only"
    hist = c.get("/api/capacity/history", headers=h).json()["history"]
    assert hist[0]["to"] == "local-only" and hist[0]["reason"] == "api test"
    assert c.post("/api/capacity/clear", headers=h, json={}).json()["state"] == "cloud-led"
    rep = c.post("/api/capacity/report", headers=h, json={"source": "claude-code", "text": "5-hour limit reached ∙ resets 3pm"}).json()
    assert rep["changed"] and rep["state"]["state"] == "local-only"


def test_the_api_serves_handoff_packs(client):
    c, h = client
    handoff.write(handoff.HandoffPack(task_id="api-1", goal="do it", notes=["n"]))
    assert [p["task_id"] for p in c.get("/api/capacity/handoff", headers=h).json()["packs"]] == ["api-1"]
    one = c.get("/api/capacity/handoff/api-1", headers=h, params={"prompt": 1}).json()
    assert one["pack"]["goal"] == "do it" and "GOAL: do it" in one["prompt"]
    assert c.get("/api/capacity/handoff/missing", headers=h).status_code == 404


def test_the_cli_reads_durations():
    from abp_cli import __main__ as cli

    assert cli._duration_s("90") == 90 and cli._duration_s("15m") == 900 and cli._duration_s("2h") == 7200 and cli._duration_s("1d") == 86400
    with pytest.raises(SystemExit):
        cli._duration_s("soon")


def test_the_cli_talks_to_the_api(client, capsys):
    from abp_cli import __main__ as cli

    c, h = client

    class Fake:
        async def _request(self, method, path, params=None, json=None):
            r = c.request(method, path, headers=h, params=params, json=json)
            r.raise_for_status()
            return r.json()

    class Args:
        json = False
        reason = "cli test"
        state = "hybrid"
        duration = "30m"
        limit = 5
        capacity_cmd = "set"

    args = Args()
    assert asyncio.run(cli._capacity(args, Fake())) == 0
    args.capacity_cmd = "status"
    assert asyncio.run(cli._capacity(args, Fake())) == 0
    out = capsys.readouterr().out
    assert "capacity: hybrid" in out and "override: hybrid by cli" in out
    args.capacity_cmd = "history"
    assert asyncio.run(cli._capacity(args, Fake())) == 0
    args.capacity_cmd = "clear"
    assert asyncio.run(cli._capacity(args, Fake())) == 0
    assert "capacity: cloud-led" in capsys.readouterr().out


def test_the_mcp_tools_call_the_routes(monkeypatch):
    from bot import mcp_server

    seen = []

    async def fake(method, path, timeout=15.0, **kw):
        seen.append((method, path, kw))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_request", fake)

    async def go():
        await mcp_server.capacity_status()
        await mcp_server.capacity_history(5)
        await mcp_server.capacity_set("local-only", "demo", 600)
        await mcp_server.capacity_clear("done")
        await mcp_server.capacity_report("claude-code", "usage limit reached")
        await mcp_server.capacity_handoff("t-9")

    asyncio.run(go())
    assert [(m, p) for m, p, _ in seen] == [("GET", "/api/capacity/state"), ("GET", "/api/capacity/history"),
                                            ("POST", "/api/capacity/override"), ("POST", "/api/capacity/clear"),
                                            ("POST", "/api/capacity/report"), ("GET", "/api/capacity/handoff/t-9")]
    assert seen[2][2]["json"] == {"state": "local-only", "reason": "demo", "actor": "mcp", "ttl_s": 600}


# ---- a bot with a fixed cloud model ------------------------------------------------------------------------------------
def test_a_fixed_cloud_model_uses_a_local_one_per_turn_and_never_changes_the_backend(monkeypatch):
    from bot.backends import native_backend as nb

    class Cloud:
        base_url = "https://api.openai.com/v1"

    class Local:
        base_url = "http://127.0.0.1:8001/v1"

    local, cloud = Local(), Cloud()
    monkeypatch.setattr(nb, "_resolve_auto_transport", lambda *a, **k: (local, "qwen", "modelmistress/qwen"))
    backend = nb.NativeAgentBackend(cloud, "gpt-x")
    assert backend._leave_cloud_for_capacity(EASY, None) is None, "cloud-led: nothing changes"
    governor.set_override("local-only", now=time.time())
    assert backend._leave_cloud_for_capacity(EASY, None) == (local, "qwen")
    assert backend.transport is cloud and backend.model == "gpt-x", "the backend itself is never assigned"


def test_concurrent_turns_on_one_fixed_model_bot_each_keep_their_own_transport(monkeypatch):
    from bot.backends import native_backend as nb
    from bot.backends.base import BackendResult

    class Cloud:
        base_url = "https://api.openai.com/v1"

    class Local:
        base_url = "http://127.0.0.1:8001/v1"

    local, cloud = Local(), Cloud()
    monkeypatch.setattr(nb, "_resolve_auto_transport", lambda *a, **k: (local, "qwen", "modelmistress/qwen"))
    backend = nb.NativeAgentBackend(cloud, "gpt-x")
    seen: dict = {}

    async def fake_ask(self, prompt, *, context=None, timeout_s=30):
        reads = []
        for _ in range(4):
            reads.append((self.transport, self.model))
            await asyncio.sleep(0.01)          # the other turns run in between
        seen[prompt] = reads
        return BackendResult(text="ok", raw={})

    monkeypatch.setattr(nb.NativeAgentBackend, "_ask", fake_ask)
    traced = []
    from bot.agent_runtime import trace

    real_begin = trace.begin
    monkeypatch.setattr(trace, "begin", lambda **kw: (traced.append(kw["model"]), real_begin(**kw))[1])

    async def go():
        governor.set_override("local-only", now=time.time())
        a = asyncio.create_task(backend.ask("A", context={"route_text": EASY}))
        await asyncio.sleep(0.005)
        governor.set_override("cloud-led", now=time.time())     # B starts after the state flipped
        b = asyncio.create_task(backend.ask("B", context={"route_text": EASY}))
        await asyncio.gather(a, b)

    assert asyncio.run(go()) is None
    assert seen["A"] == [(local, "qwen")] * 4, "turn A ran on the local model throughout"
    assert seen["B"] == [(cloud, "gpt-x")] * 4, "turn B ran on its own cloud model throughout"
    assert backend.transport is cloud and backend.model == "gpt-x"
    assert sorted(traced) == ["gpt-x", "qwen"], "the trace records the model each turn actually used"


def test_a_fixed_model_stays_when_reroute_is_off_or_nothing_local_exists(monkeypatch):
    from bot.backends import native_backend as nb
    from bot.backends.base import BackendError
    from bot.config import config

    class Cloud:
        base_url = "https://api.openai.com/v1"

    def none_local(*a, **k):
        raise BackendError("no free model")

    governor.set_override("local-only", now=time.time())
    cloud = Cloud()
    backend = nb.NativeAgentBackend(cloud, "gpt-x")
    monkeypatch.setattr(nb, "_resolve_auto_transport", none_local)
    assert backend._leave_cloud_for_capacity(EASY, None) is None
    monkeypatch.setitem(config._data, "capacity", {"reroute_fixed_models": False})
    monkeypatch.setattr(nb, "_resolve_auto_transport", lambda *a, **k: (object(), "x", "modelmistress/x"))
    assert backend._leave_cloud_for_capacity(EASY, None) is None
    assert backend.transport is cloud


def test_failover_hops_use_the_router_while_capacity_is_short(monkeypatch):
    from bot.backends import native_backend as nb

    monkeypatch.setattr(nb, "_resolve_fallback_transport", lambda iid: None)
    calls = []

    def fake(task, **k):
        calls.append(k["mode"])
        if len(calls) > 1:
            raise nb.BackendError("done")
        return "t", "m", "modelmistress/m"

    monkeypatch.setattr(nb, "_resolve_auto_transport", fake)
    assert list(nb._failover_hops(None, EASY)) == []                      # cloud-led, auto_failover off: no hops
    governor.set_override("local-only", now=time.time())
    assert list(nb._failover_hops(None, EASY)) == [("t", "m")] and calls[0] == "failover"
