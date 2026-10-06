"""ABP with no rule engine installed: rules are off, and nothing else changes.

`ABP_NATIVE=0` is how this is reached for real, and it is also what a fresh checkout without
`native/abp-rules` built looks like. The claim is that a missing optional feature is a missing
feature, not a broken one: rules report a clear reason, the API says so with a 503 rather than
storing something that could never run, and the three integration points carry on exactly as they
did before rules existed.

These must pass whether or not the extension is built: everything here works with `ABP_NATIVE=0`,
and the one case that has to create a rule before it can lose the engine again skips when there is
no engine to create it with.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances, db, routines, rules
from bot.agent_runtime import hooks
from bot.dashboard.server import build_app
from bot.router import router

H = {"X-Dashboard-Token": "test-token"}
TEMPLATE = "Summarise the open pull requests in {{repo}} and list anything blocked."
PARAMS = {"repo": {"description": "owner/name"}}


@pytest.fixture
def no_engine(monkeypatch):
    """Exactly what an installation without the extension sees."""
    monkeypatch.setenv("ABP_NATIVE", "0")
    rules.engine.reset_cache()
    assert rules.available() is False
    yield
    rules.engine.reset_cache()


@pytest.fixture
def iid(temp_db):
    return bot_instances.create_instance(name="r", platform="telegram", backend="api",
                                         credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
                                         allowed_user_ids=[1])


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


# ---- what ABP says about it -----------------------------------------------------------------------
def test_rules_report_a_clear_reason_rather_than_an_import_error(no_engine):
    status = rules.status()
    assert status["available"] is False and status["backend"] == "disabled"
    assert "ABP_NATIVE=0" in status["reason"]
    assert "build.py" in rules.DISABLED_MESSAGE, "the message has to say how to fix it"
    assert rules.limits(), "the limits are still published, so a page can show what a rule would be held to"


def test_asking_for_an_answer_without_an_engine_is_an_explicit_error(no_engine):
    with pytest.raises(rules.RulesDisabled) as caught:
        rules.matches("true", {})
    assert caught.value.phase == "disabled" and "not installed" in str(caught.value)
    assert isinstance(caught.value, rules.RuleError), "one error shape, so a caller catches one thing"


# ---- the integration points are untouched ----------------------------------------------------------
def test_a_routine_still_runs_with_no_engine_available(no_engine, iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    assert routines.engine_available() is False
    assert routines.scheduled_run_allowed(sched_id, prompt="Summarise o/r") == (True, "")


def test_hooks_still_run_with_no_engine_available(no_engine, iid, tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("import json, sys\njson.loads(sys.stdin.read() or '{}')\n"
                      "print(json.dumps({'decision': 'deny', 'reason': 'still here'}))\n", encoding="utf-8")
    db.add_agent_hook(event="PreToolUse", matcher="Bash", command=f'"{script.as_posix()}"')
    assert asyncio.run(hooks.run_pre_tool_use("Bash", {}, instance_id=iid)) == ("deny", "still here")


def test_routing_is_exactly_what_it_was_with_no_engine_available(no_engine, iid, temp_db):
    before = router.resolve_chain("quick_question", None, instance_id=iid)
    assert router._rule_route("hello", "quick_question", iid, 1) == (None, None)
    assert router.resolve_chain("quick_question", None, instance_id=iid) == before


def test_a_broken_rules_package_cannot_break_a_turn(no_engine, monkeypatch, iid, temp_db):
    """`router._rule_route` wraps everything, because it runs on every message: whatever is wrong
    with the rules package, the router still routes."""
    def explode(*a, **kw):
        raise RuntimeError("the rules package is broken")

    monkeypatch.setattr(rules.store, "enabled_count", explode)
    assert router._rule_route("hello", "quick_question", iid, 1) == (None, None)


# ---- the API --------------------------------------------------------------------------------------
def test_the_status_endpoint_explains_the_missing_engine(client, no_engine):
    status = client.get("/api/rules/status", headers=H).json()
    assert status["available"] is False and status["backend"] == "disabled"
    assert status["reason"] and status["kinds"] == ["condition", "filter", "route"]


def test_writes_are_refused_with_the_reason_rather_than_stored(client, no_engine):
    for method, path, body in (
        ("post", "/api/rules", {"name": "x", "source": "true", "kind": "condition"}),
        ("post", "/api/rules/validate", {"source": "true", "kind": "condition"}),
        ("post", "/api/rules/test", {"source": "true", "kind": "condition", "context": {}}),
    ):
        answered = client.request(method, path, headers=H, json=body)
        assert answered.status_code == 503, f"{path} answered {answered.status_code}"
        assert "not installed" in answered.json()["detail"]
    assert client.get("/api/rules").status_code == 401, "reads still need a token"
    assert client.get("/api/rules", headers=H).json()["rules"] == [], "and none of them were stored"


def test_reading_rules_still_works_so_the_configuration_is_not_lost(client, no_engine, monkeypatch):
    """A stored rule outlives the engine: an install that loses its native module keeps what a
    person wrote and stops enforcing it, rather than losing it. Creating one in the first place
    needs the engine, so this half of the contract needs the extension to be built."""
    rules.engine.reset_cache()
    monkeypatch.delenv("ABP_NATIVE", raising=False)
    rules.engine.reset_cache()
    if not rules.available():
        pytest.skip("abp_rules is not built, so no rule can be created to leave behind")
    rule = rules.store.create("kept", "true", "condition", target="routine:digest")
    monkeypatch.setenv("ABP_NATIVE", "0")
    rules.engine.reset_cache()
    listed = client.get("/api/rules", headers=H).json()
    assert [r["name"] for r in listed["rules"]] == ["kept"]
    assert listed["rules"][0]["usable"] is False and "not installed" in listed["rules"][0]["problem"]
    assert rules.store.get(rule.id).source == "true"