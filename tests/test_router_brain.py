"""The learning router: it records how it thinks, learns from outcomes and feedback, can be trained, and its policy
can be edited, versioned and restored. Also how a bot on `auto` uses it: failover to the next pick when a model is
refused, keeping the one that worked, and re-routing when its model is resting."""
from __future__ import annotations

import asyncio
import json
import random
import time

import pytest

from bot import model_router
from bot.agent_runtime import usage_limits
from bot.model_pricing import _memory_cache as catalog_cache
from bot.router_brain import learn, policy, store

CATALOG = {"p": {"id": "p", "models": {
    "big": {"id": "big", "tool_call": True, "reasoning": True, "modalities": {"input": ["text", "image"]}, "limit": {"context": 200000, "output": 8000},
            "cost": {"input": 3, "output": 15}},
    "cheap": {"id": "cheap", "tool_call": True, "modalities": {"input": ["text"]}, "limit": {"context": 32000, "output": 4000}, "cost": {"input": 0, "output": 0}},
    "cheap2": {"id": "cheap2", "tool_call": True, "modalities": {"input": ["text"]}, "limit": {"context": 32000, "output": 4000}, "cost": {"input": 0, "output": 0}},
}}}
TRIVIAL = "what is 2 + 2?"


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setitem(catalog_cache, "data", CATALOG)
    usage_limits._blocked_until.clear()
    policy.invalidate()
    learn._model_cache.clear()
    yield
    usage_limits._blocked_until.clear()
    policy.invalidate()


def top(task=TRIVIAL, cands=("p/cheap", "p/cheap2")):
    return model_router.recommend(task, candidates=list(cands))[1][0].model


# ---- failures are sorted into kinds -----------------------------------------------------------------------------------
@pytest.mark.parametrize("text,kind", [
    ("openai-compatible transport (https://x) returned 429: slow down", "rate_limited"),
    ("returned 403: this model is only available to approved agentic harnesses", "gated"),
    ("returned 404: No endpoints found for qwen/qwen3-coder:free", "gone"),
    ("returned 401: invalid api key", "auth"),
    ("returned 402: insufficient credits", "payment"),
    ("returned 400: This model's maximum context length is 32768 tokens", "context"),
    ("returned 400: this model does not support tools", "no_tools"),
    ("returned 503: upstream overloaded", "server"),
    ("openai-compatible transport (https://x) timed out after 30s", "timeout"),
])
def test_failures_are_sorted_into_kinds(text, kind):
    assert learn.classify_error(text) == kind


# ---- learning from outcomes -------------------------------------------------------------------------------------------
def test_a_rate_limit_rests_the_model_and_the_rest_doubles_on_repeat():
    learn.observe("p/cheap", ok=False, error="returned 429: slow down")
    first = learn.cooldown("p/cheap")
    assert first["kind"] == "rate_limited" and 100 < first["until"] - time.time() <= 120
    learn.observe("p/cheap", ok=False, error="returned 429: slow down")
    second = learn.cooldown("p/cheap")
    assert second["strikes"] == 2 and 200 < second["until"] - time.time() <= 240
    assert top() == "p/cheap2", "a resting model is not picked"
    _, _, skipped = model_router.recommend(TRIVIAL, candidates=["p/cheap", "p/cheap2"])
    assert any("p/cheap: resting" in s and "rate limited" in s for s in skipped)
    assert any(e["kind"] == "cooldown" and "p/cheap" in e["message"] for e in learn.events())


def test_one_403_is_a_short_rest_but_repeats_earn_the_long_one():
    learn.observe("p/cheap", ok=False, error="returned 403: not allowed")
    assert learn.cooldown("p/cheap")["until"] - time.time() <= 3600
    for _ in range(2):
        learn.observe("p/cheap", ok=False, error="returned 403: not allowed")
    assert learn.cooldown("p/cheap")["until"] - time.time() > 5 * 86400


def test_a_bad_key_rests_the_whole_provider():
    learn.observe("p/cheap", ok=False, error="returned 401: invalid api key")
    assert learn.cooldown("p/cheap2")["key"] == "p/*"


def test_a_context_overflow_is_not_held_against_the_model():
    learn.observe("p/cheap", ok=False, error="returned 400: maximum context length exceeded")
    s = learn.stats("p/cheap")
    assert s["fail"] == 0 and learn.cooldown("p/cheap") is None


def test_reliability_is_learned_and_decides_between_equals():
    for _ in range(6):
        learn.observe("p/cheap2", ok=True, latency_ms=800)
    learn.observe("p/cheap", ok=False, error="returned 500: boom")
    learn.clear_cooldown("p/cheap")
    ranked = model_router.recommend(TRIVIAL, candidates=["p/cheap", "p/cheap2"])[1]
    assert ranked[0].model == "p/cheap2" and ranked[0].reliability > ranked[1].reliability
    assert ranked[0].latency_ms == pytest.approx(800) and any("per call" in r for r in ranked[0].reasons)


def test_a_success_after_failures_is_logged_as_a_recovery():
    for _ in range(2):
        learn.observe("p/cheap", ok=False, error="returned 500: boom")
    learn.observe("p/cheap", ok=True)
    assert any(e["kind"] == "recovered" for e in learn.events())


def test_old_outcomes_fade():
    learn.observe("p/cheap", ok=False, error="returned 500: boom")
    store.execute("UPDATE model_stats SET updated = updated - ?", (28 * 86400,))   # two half-lives ago
    assert learn.stats("p/cheap")["fail"] == pytest.approx(0.25, rel=0.01)


def test_learning_can_be_switched_off():
    pol = policy.current()
    pol["learning"]["enabled"] = False
    policy.save(pol, note="off")
    learn.observe("p/cheap", ok=False, error="returned 429: x")
    assert learn.cooldown("p/cheap") is None


# ---- decisions are recorded with their reasoning ----------------------------------------------------------------------
def test_route_records_the_whole_decision():
    d = model_router.route(TRIVIAL, candidates=["p/cheap", "p/big"], instance_id=7)
    row = store.one("SELECT * FROM decisions WHERE id = ?", (d.id,))
    detail = json.loads(row["detail"])
    assert row["chosen"] == d.chosen == "p/cheap" and row["status"] == "pending" and row["instance_id"] == 7
    assert row["task_excerpt"] == TRIVIAL and row["task_class"] == "trivial"
    assert detail["classification"]["task_class"] == "trivial" and len(detail["candidates"]) == 2
    c = detail["candidates"][0]
    assert set(c["components"]) == set(policy.COMPONENTS) and sum(c["contributions"].values()) == pytest.approx(c["score"], abs=1e-3)
    learn.observe("p/cheap", ok=True, latency_ms=1200, tokens=50, decision_id=d.id)
    learn.observe("p/cheap", ok=True, latency_ms=900, tokens=30, decision_id=d.id)
    row = store.one("SELECT * FROM decisions WHERE id = ?", (d.id,))
    assert row["status"] == "ok" and row["latency_ms"] == 1200 and row["calls"] == 2 and row["tokens"] == 80


def test_route_skips_excluded_and_unusable_and_says_so():
    d = model_router.route(TRIVIAL, candidates=["p/cheap", "p/cheap2", "p/big"], exclude={"p/cheap"}, usable=lambda r: r != "p/cheap2")
    assert d.chosen == "p/big"
    assert any("already tried" in n for n in d.notes) and any("not configured" in n for n in d.notes)


def test_exploration_sometimes_tries_the_less_proven_model_and_says_so():
    for _ in range(3):
        learn.observe("p/cheap", ok=True)
    pol = policy.current()
    pol["learning"]["explore"] = 1.0
    policy.save(pol)
    picks = {model_router.route(TRIVIAL, candidates=["p/cheap", "p/cheap2"], rng=random.Random(i), record=False).chosen for i in range(40)}
    assert picks == {"p/cheap", "p/cheap2"}
    d = next(d for d in (model_router.route(TRIVIAL, candidates=["p/cheap", "p/cheap2"], rng=random.Random(i)) for i in range(40)) if d.explored)
    assert d.greedy == "p/cheap" and any("exploring" in n for n in d.notes)
    assert store.one("SELECT explored FROM decisions WHERE id = ?", (d.id,))["explored"] == 1


def test_advice_is_recorded_but_not_as_a_pending_decision():
    cls, ranked, skipped = model_router.recommend(TRIVIAL, candidates=["p/cheap"])
    model_router.record_advice(TRIVIAL, cls, ranked, skipped)
    assert store.one("SELECT mode, status FROM decisions") == {"mode": "advise", "status": "advice"}


def test_task_text_is_not_kept_when_the_policy_says_so():
    pol = policy.current()
    pol["record_task_text"] = False
    policy.save(pol)
    d = model_router.route(TRIVIAL, candidates=["p/cheap"])
    assert store.one("SELECT task_excerpt FROM decisions WHERE id = ?", (d.id,))["task_excerpt"] is None


# ---- feedback and training --------------------------------------------------------------------------------------------
def test_feedback_moves_quality_and_a_flipped_vote_is_not_counted_twice():
    d = model_router.route(TRIVIAL, candidates=["p/cheap"])
    base = model_router.recommend(TRIVIAL, candidates=["p/cheap"])[1][0].quality
    learn.feedback(d.id, rating=-1)
    r = model_router.recommend(TRIVIAL, candidates=["p/cheap"])[1][0]
    assert r.quality < base and r.quality_source == "learned"
    learn.feedback(d.id, rating=1)
    s = learn.stats("p/cheap", "trivial")
    assert s["up"] == pytest.approx(1) and s["down"] == pytest.approx(0)
    assert model_router.recommend(TRIVIAL, candidates=["p/cheap"])[1][0].quality > base


def test_correcting_the_class_trains_the_classifier():
    task = "draft the quarterly newsletter for our members"
    assert model_router.classify(task).task_class == "trivial"
    d = model_router.route(task, candidates=["p/cheap"])
    out = learn.feedback(d.id, correct_class="hard_reasoning")
    assert out["learned"] and learn.examples()[0]["source"] == "feedback"
    for text in ("draft the annual newsletter for members", "draft a newsletter about the members meeting"):
        learn.add_example(text, task_class="hard_reasoning")
    c = model_router.classify(task)
    assert c.task_class == "hard_reasoning" and c.source == "classifier" and c.keyword_class == "trivial"
    assert c.scores["hard_reasoning"] >= 0.6


def test_a_similar_example_boosts_the_model_it_prefers():
    task = "tell me a joke about cats"
    assert top(task) == "p/cheap"
    learn.add_example("tell me a joke about dogs", preferred_model="p/cheap2")
    r = model_router.recommend(task, candidates=["p/cheap", "p/cheap2"])[1]
    assert r[0].model == "p/cheap2" and any("training example" in a["why"] for a in r[0].adjustments)


def test_bad_feedback_is_refused():
    d = model_router.route(TRIVIAL, candidates=["p/cheap"])
    with pytest.raises(ValueError):
        learn.feedback(d.id, correct_class="poetry")
    with pytest.raises(ValueError):
        learn.feedback(d.id, preferred_model="nomodel")
    with pytest.raises(KeyError):
        learn.feedback(99999, rating=1)


# ---- the policy ---------------------------------------------------------------------------------------------------------
def test_rules_block_pin_prefer_and_avoid():
    pol = policy.current()
    pol["rules"] = [{"kind": "block", "model": "p/cheap"}]
    policy.save(pol)
    _, ranked, skipped = model_router.recommend(TRIVIAL, candidates=["p/cheap", "p/cheap2"])
    assert [r.model for r in ranked] == ["p/cheap2"] and any("blocked by a rule" in s for s in skipped)
    pol["rules"] = [{"kind": "pin", "model": "p/big"}]
    policy.save(pol)
    assert top(cands=("p/cheap", "p/big")) == "p/big"
    pol["rules"] = [{"kind": "pin", "model": "p/big", "classes": ["coding"]}]
    policy.save(pol)
    assert top(cands=("p/cheap", "p/big")) == "p/cheap", "a rule for another class does not apply"
    pol["rules"] = [{"kind": "avoid", "model": "p/cheap", "boost": 0.5}]
    policy.save(pol)
    assert top() == "p/cheap2"
    pol["rules"] = [{"kind": "block", "model": "p/*", "until": time.time() - 10}]
    policy.save(pol)
    assert top() == "p/cheap", "an expired rule is ignored"


def test_keywords_and_weights_change_how_it_thinks():
    assert model_router.classify("tidy up the invoices").task_class == "trivial"
    pol = policy.current()
    pol["keywords"]["bulk"] = ["invoices"]
    pol["weights"]["hard_reasoning"] = {"quality": 0, "economy": 1, "headroom": 0, "reliability": 0, "speed": 0}
    policy.save(pol)
    c = model_router.classify("tidy up the invoices")
    assert c.task_class == "bulk" and any("invoices" in r for r in c.reasons)
    hard = "Design the architecture and analyse the trade-offs of a queue-based ingestion pipeline for our repository. " * 3
    assert top(hard, ("p/big", "p/cheap")) == "p/cheap", "with only economy counting, the free model wins even hard tasks"


def test_a_malformed_policy_is_refused_with_a_reason():
    for bad, why in [({"weights": {"poetry": {}}}, "unknown task class"), ({"weights": {"coding": {"quality": 2}}}, "between"),
                     ({"rules": [{"kind": "ban", "model": "p/x"}]}, "kind"), ({"rules": [{"kind": "pin", "model": "x"}]}, "provider/model"),
                     ({"learning": {"explore": 5}}, "between"), ({"learning": {"nope": 1}}, "unknown learning setting"),
                     ({"weights": {"coding": {k: 0 for k in policy.COMPONENTS}}}, "above zero")]:
        with pytest.raises(ValueError, match=why):
            policy.validate(bad)


def test_every_change_is_a_version_with_its_diff_and_can_be_restored():
    assert policy.version() == 0
    pol = policy.current()
    pol["learning"]["explore"] = 0.25
    r1 = policy.save(pol, note="explore more")
    assert r1["version"] == 1 and r1["changes"] == ["learning.explore: 0.1 -> 0.25"]
    assert policy.save(pol)["changes"] == [], "saving the same thing twice is not a new version"
    pol["sticky"] = False
    policy.save(pol, note="re-route each turn")
    hist = policy.history()
    assert [h["version"] for h in hist] == [2, 1] and hist[0]["changes"] == ["sticky: true -> false"]
    back = policy.rollback(1)
    assert back["version"] == 3 and policy.current()["sticky"] is True and policy.current()["learning"]["explore"] == 0.25
    policy.rollback(0)
    assert policy.current() == policy.DEFAULT
    assert any(e["kind"] == "policy" for e in learn.events())


# ---- a bot on auto ------------------------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=httpx.Request("POST", "http://x"), response=self)

    def json(self):
        return self._data


def _auto_backend(monkeypatch, answers):
    """A bot on auto whose provider answers per model from `answers` ({model: status})."""
    from bot import providers
    from bot.backends.custom_model_backend import CustomModelBackend

    providers.set_provider("p", base_url="https://p.example/v1", api_key="unused")
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["p/cheap", "p/cheap2", "p/big"])
    sent = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(json["model"])
            status = answers.get(json["model"], 200)
            if status != 200:
                return _Resp(status, {"error": {"message": f"model {json['model']} refused"}})
            return _Resp(200, {"choices": [{"message": {"role": "assistant", "content": f"answer from {json['model']}"}}]})

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", Client)
    return CustomModelBackend(provider_name=None, model_id="auto", base_url=""), sent


def _instance():
    from bot import bot_instances

    return bot_instances.create_instance(name="auto-bot", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])


def test_an_auto_bot_fails_over_when_its_pick_is_refused_and_keeps_the_one_that_worked(temp_db, monkeypatch, tmp_path):
    backend, sent = _auto_backend(monkeypatch, {"cheap": 403})
    ctx = {"cwd": str(tmp_path / "ws"), "instance_id": _instance()}
    result = asyncio.run(backend.ask(TRIVIAL, context=ctx))
    assert result.text == "answer from cheap2" and sent == ["cheap", "cheap2"]
    rows = store.rows("SELECT id, mode, chosen, status, error_kind, parent_id FROM decisions ORDER BY id")
    assert [(r["mode"], r["chosen"], r["status"]) for r in rows] == [("auto", "p/cheap", "failed"), ("failover", "p/cheap2", "ok")]
    assert rows[0]["error_kind"] == "gated" and rows[1]["parent_id"] == rows[0]["id"]
    assert learn.cooldown("p/cheap")["kind"] == "gated"
    # The next turn stays on the model that worked, and is recorded too.
    asyncio.run(backend.ask("and 3 + 3?", context=ctx))
    assert sent[-1] == "cheap2"
    last = store.rows("SELECT mode, chosen, status FROM decisions ORDER BY id DESC LIMIT 1")[0]
    assert last == {"mode": "sticky", "chosen": "p/cheap2", "status": "ok"}


def test_an_auto_bot_reroutes_when_its_model_starts_resting(temp_db, monkeypatch, tmp_path):
    backend, sent = _auto_backend(monkeypatch, {})
    ctx = {"cwd": str(tmp_path / "ws"), "instance_id": _instance()}
    asyncio.run(backend.ask(TRIVIAL, context=ctx))
    assert sent == ["cheap"]
    learn.set_cooldown("p/cheap", 600, kind="manual", reason="test", manual=True)
    asyncio.run(backend.ask("and 3 + 3?", context=ctx))
    assert sent[-1] == "cheap2"
    assert store.rows("SELECT mode FROM decisions ORDER BY id DESC LIMIT 1")[0]["mode"] == "reroute"


def test_a_fixed_model_bot_still_teaches_the_router(temp_db, monkeypatch, tmp_path):
    from bot import providers
    from bot.backends.custom_model_backend import CustomModelBackend

    _auto_backend(monkeypatch, {"big": 429})
    providers.set_provider("p", base_url="https://p.example/v1", api_key="unused")
    backend = CustomModelBackend(provider_name="p", model_id="big", base_url="https://p.example/v1")
    with pytest.raises(Exception):
        asyncio.run(backend.ask(TRIVIAL, context={"cwd": str(tmp_path / "ws"), "instance_id": _instance()}))
    assert learn.cooldown("p/big")["kind"] == "rate_limited"
    assert store.rows("SELECT COUNT(*) AS n FROM decisions")[0]["n"] == 0, "a fixed model makes no routing decisions"


# ---- the API ------------------------------------------------------------------------------------------------------------
def test_the_api(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setattr(model_router, "candidate_models", lambda: ["p/cheap", "p/cheap2"])
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/router/overview").status_code == 401
    d = model_router.route(TRIVIAL, candidates=["p/cheap", "p/cheap2"])
    learn.observe("p/cheap", ok=False, error="returned 429: x", decision_id=d.id)

    o = c.get("/api/router/overview", headers=h).json()
    assert o["decisions"] == 1 and o["failed"] == 1 and o["resting"][0]["key"] == "p/cheap" and o["timeline"]
    assert o["heatmap"]["p/cheap"]["trivial"]["failed"] == 1
    m = c.get("/api/router/models", headers=h).json()
    assert m["models"][0]["model"] == "p/cheap" and m["models"][0]["cooldown"]["kind"] == "rate_limited" and m["never_used"] == ["p/cheap2"]
    lst = c.get("/api/router/decisions?status=failed", headers=h).json()["decisions"]
    assert [x["id"] for x in lst] == [d.id] and lst[0]["runner_up"] == "p/cheap2"
    one = c.get(f"/api/router/decisions/{d.id}", headers=h).json()
    assert one["detail"]["classification"]["task_class"] == "trivial" and one["events"][0]["kind"] == "cooldown"
    assert c.get("/api/router/decisions/9999", headers=h).status_code == 404

    fb = c.post(f"/api/router/decisions/{d.id}/feedback", headers=h, json={"rating": -1, "preferred_model": "p/cheap2"}).json()
    assert len(fb["learned"]) == 2
    assert c.post(f"/api/router/decisions/{d.id}/feedback", headers=h, json={"correct_class": "poetry"}).status_code == 400

    sim = c.post("/api/router/simulate", headers=h, json={"task": TRIVIAL}).json()
    assert sim["candidates"][0]["model"] == "p/cheap2" and any("resting" in s for s in sim["skipped"]) and "Task looks like" in sim["text"]
    assert store.rows("SELECT COUNT(*) AS n FROM decisions")[0]["n"] == 1, "a simulation records nothing"

    pol = c.get("/api/router/policy", headers=h).json()
    assert pol["version"] == 0 and pol["task_classes"][0] == "trivial"
    p = pol["policy"]
    p["learning"]["explore"] = 0.2
    saved = c.put("/api/router/policy", headers=h, json={"policy": p, "note": "more exploring"}).json()
    assert saved["version"] == 1
    p["weights"]["coding"]["quality"] = 7
    assert c.put("/api/router/policy", headers=h, json={"policy": p}).status_code == 400
    assert c.get("/api/router/policy/history", headers=h).json()["versions"][0]["note"] == "more exploring"
    assert c.post("/api/router/policy/rollback", headers=h, json={"version": 0}).json()["version"] == 2
    assert c.post("/api/router/policy/rollback", headers=h, json={"version": 42}).status_code == 404

    ex = c.post("/api/router/examples", headers=h, json={"text": "tag every invoice", "task_class": "bulk"}).json()
    assert any(x["id"] == ex["id"] for x in c.get("/api/router/examples", headers=h).json()["examples"])
    assert c.post("/api/router/examples", headers=h, json={"text": "x"}).status_code == 400
    assert c.delete(f"/api/router/examples/{ex['id']}", headers=h).json() == {"ok": True}

    assert c.post("/api/router/models/release", headers=h, json={"model": "p/cheap"}).json() == {"released": True}
    assert c.post("/api/router/models/rest", headers=h, json={"model": "p/cheap2", "seconds": 60}).json()["key"] == "p/cheap2"
    assert c.post("/api/router/models/rest", headers=h, json={"model": "nope"}).status_code == 400
    assert c.post("/api/router/models/forget", headers=h, json={"model": "p/cheap"}).json() == {"ok": True}
    kinds = {e["kind"] for e in c.get("/api/router/events", headers=h).json()["events"]}
    assert {"cooldown", "feedback", "policy", "example", "cooldown_cleared", "reset"} <= kinds
    assert c.post("/api/router/reset", headers=h, json={}).json() == {"ok": True}
    assert c.get("/api/router/overview", headers=h).json()["decisions"] == 0


def test_routing_classifies_the_persons_message_not_the_bots_instructions(temp_db, monkeypatch, tmp_path):
    """bot/router.py puts a bot's standing instructions in front of the prompt; routing on that made every task of a bot
    with long instructions look like a long coding request (seen live with an imported Hermes SOUL.md)."""
    backend, _sent = _auto_backend(monkeypatch, {})
    instructions = "You are a careful assistant. Refactor code, write tests and review every commit. " * 12
    ctx = {"cwd": str(tmp_path / "ws"), "instance_id": _instance(), "route_text": TRIVIAL}
    asyncio.run(backend.ask(f"{instructions}\n\n{TRIVIAL}", context=ctx))
    row = store.rows("SELECT task_class, task_excerpt FROM decisions")[0]
    assert row == {"task_class": "trivial", "task_excerpt": TRIVIAL}


def test_the_router_hands_the_backend_the_persons_own_message():
    import inspect

    from bot import router as bot_router

    assert 'context.setdefault("route_text", prompt)' in inspect.getsource(bot_router)
