"""Rules as stored data, and the API over them.

The API tests are round trips against the real FastAPI app and the real SQLite table: a rule created
through `/api/rules` is the row `bot/rules/integration.py` reads on the next turn, and a rule
validated through `/api/rules/validate` is compiled by the same `abp_rules` the scheduler will use.
Only the dashboard token is arranged, not anything the test is about.
"""
from __future__ import annotations

import pytest

abp_rules = pytest.importorskip("abp_rules", reason="abp_rules is not built; run native/abp-rules/build.py --venv ...")

from fastapi.testclient import TestClient  # noqa: E402

from bot import db, rules  # noqa: E402
from bot.dashboard.server import build_app  # noqa: E402

H = {"X-Dashboard-Token": "test-token"}
WEEKDAY = 'ctx.weekday < 5 && ctx.message.contains("digest")'


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


# ---- the model -------------------------------------------------------------------------------------
def test_a_rule_is_a_name_some_source_and_what_it_decides(temp_db):
    rule = rules.store.create("weekday-digest", WEEKDAY, "condition", description="only on a weekday",
                              target="routine:digest")
    assert rule.id and rule.name == "weekday-digest" and rule.kind == "condition"
    assert rule.enabled is True and rule.target == "routine:digest" and rule.instance_id is None
    assert rules.store.get(rule.id).as_dict() == rule.as_dict()
    assert rules.store.get_by_name("WEEKDAY-DIGEST").id == rule.id       # names are lowercased, not rejected


def test_a_rule_is_compiled_before_it_is_stored_with_its_position(temp_db):
    with pytest.raises(rules.RuleStoreError) as caught:
        rules.store.create("broken", "let a = 1;\nlet b = ;", "condition")
    assert "line 2" in str(caught.value)
    assert rules.store.listing() == [], "a rule that does not compile is not half-saved"


def test_the_names_the_model_accepts(temp_db):
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("Not A Name", "true", "condition")
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("bad-kind-name", "true", "spell")
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("bad-target", "true", "condition", target="something else")
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("wrong-shape", "true", "condition", target="model")       # only a route picks a target
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("wrong-shape", '"x"', "route", target="routine:digest")     # a route does not gate


def test_two_rules_cannot_share_a_name(temp_db):
    rules.store.create("taken", "true", "condition")
    with pytest.raises(rules.RuleStoreError):
        rules.store.create("taken", "true", "condition")


def test_updating_leaves_the_stored_rule_alone_when_the_new_source_is_broken(temp_db):
    rule = rules.store.create("guard", "true", "condition")
    with pytest.raises(rules.RuleStoreError):
        rules.store.update(rule.id, source="let x = ;")
    assert rules.store.get(rule.id).source == "true"


def test_a_rule_can_be_turned_off_without_being_deleted(temp_db):
    rule = rules.store.create("guard", "true", "condition")
    rules.store.set_enabled(rule.id, False)
    assert rules.store.get(rule.id).enabled is False
    assert rules.store.enabled_count("condition") == 0, "a disabled rule decides nothing"
    assert rules.store.bound("routine:x", "condition") == []
    rules.store.set_enabled(rule.id, True)
    assert rules.store.enabled_count("condition") == 1


def test_deleting_a_rule_takes_its_record_with_it(temp_db):
    rule = rules.store.create("guard", "true", "condition")
    assert rules.store.delete(rule.id).name == "guard"
    assert rules.store.get(rule.id) is None
    with pytest.raises(rules.RuleStoreError):
        rules.store.delete(rule.id)


def test_a_rule_remembers_the_last_thing_that_went_wrong(temp_db):
    rule = rules.store.create("guard", "true", "condition")
    rules.store.note_result(rule.id)
    rules.store.note_result(rule.id, error="line 1, column 1")
    described = rules.store.history(rule.id)[0]
    assert described["checks"] == 2 and described["last_error"] == "line 1, column 1"


def test_listing_can_be_narrowed_by_everything_it_can_be_narrowed_by(temp_db):
    rules.store.create("a-filter", 'ctx.tool == "Bash"', "filter", target="hook:PreToolUse:Bash")
    rules.store.create("b-route", '"cli"', "route", target="backend")
    rules.store.create("c-loose", "true", "condition")
    assert [r.name for r in rules.store.listing()] == ["a-filter", "b-route", "c-loose"]
    assert [r.name for r in rules.store.listing(kind="filter")] == ["a-filter"]
    assert [r.name for r in rules.store.listing(target="backend")] == ["b-route"]
    # A rule with no instance applies to every instance, exactly as agent_hooks does; one with an
    # instance applies to that instance and no other.
    rules.store.update(rules.store.get_by_name("c-loose").id, instance_id=7)
    assert [r.name for r in rules.store.listing(instance_id=7)] == ["a-filter", "b-route", "c-loose"]
    assert [r.name for r in rules.store.listing(instance_id=8)] == ["a-filter", "b-route"]


def test_describe_says_whether_the_rule_can_run_here(temp_db):
    rule = rules.store.create("guard", WEEKDAY, "condition")
    described = rules.store.describe(rule)
    assert described["usable"] is True and described["problem"] == ""
    assert described["engine"] == "abp_rules"
    row = db.get_conn().execute("UPDATE rules SET source=? WHERE id=?", ("let y = ;", rule.id))
    row = db.get_conn().execute("SELECT * FROM rules WHERE id=?", (rule.id,)).fetchone()
    described = rules.store.describe(rules.store._row(row))
    assert described["usable"] is False and "line 1" in described["problem"]


# ---- the API ---------------------------------------------------------------------------------------
def test_the_routes_need_a_token(client):
    for method, path in (("get", "/api/rules"), ("get", "/api/rules/status"), ("get", "/api/rules/1"),
                         ("post", "/api/rules"), ("put", "/api/rules/1"), ("delete", "/api/rules/1"),
                         ("post", "/api/rules/validate"), ("post", "/api/rules/test"),
                         ("post", "/api/rules/1/test"), ("post", "/api/rules/1/enabled")):
        assert client.request(method, path).status_code == 401, f"{method.upper()} {path} is open without a token"


def test_a_paired_phone_may_read_but_not_write(client):
    """Reading follows the dashboard's normal auth; a rule changes what runs unattended, which is
    the dashboard token's alone."""
    _key_id, key = db.create_api_key("my-phone", permission_tier="standard")
    phone = {"X-Dashboard-Token": key}
    rules.store.create("guard", "true", "condition")
    assert client.get("/api/rules", headers=phone).status_code == 200
    assert client.post("/api/rules/validate", headers=phone, json={"source": "true"}).status_code == 401
    assert client.post("/api/rules", headers=phone, json={"name": "x", "source": "true"}).status_code == 401
    assert client.delete("/api/rules/1", headers=phone).status_code == 401
    assert rules.store.get(1) is not None, "nothing was actually changed by the refused calls"


def test_status_says_what_is_installed_and_what_it_refuses(client):
    status = client.get("/api/rules/status", headers=H).json()
    assert status["available"] is True and status["backend"] == "abp_rules"
    assert status["kinds"] == ["condition", "filter", "route"]
    assert status["limits"]["max_operations"] > 0 and "engine_version" in status


def test_the_whole_crud_life_cycle(client):
    made = client.post("/api/rules", headers=H, json={
        "name": "weekday-digest", "source": WEEKDAY, "kind": "condition",
        "description": "only on a weekday", "target": "routine:digest"})
    assert made.status_code == 201
    rule_id = made.json()["rule"]["id"]
    assert made.json()["rule"]["usable"] is True

    listed = client.get("/api/rules", headers=H).json()
    assert [r["name"] for r in listed["rules"]] == ["weekday-digest"]
    assert listed["status"]["available"] is True

    detail = client.get(f"/api/rules/{rule_id}", headers=H).json()["rule"]
    assert detail["source"] == WEEKDAY and detail["target"] == "routine:digest"

    edited = client.put(f"/api/rules/{rule_id}", headers=H, json={"description": "weekdays only"})
    assert edited.status_code == 200 and edited.json()["rule"]["description"] == "weekdays only"
    assert edited.json()["rule"]["source"] == WEEKDAY, "an edit only changes what it was given"

    paused = client.post(f"/api/rules/{rule_id}/enabled", headers=H, json={"enabled": False})
    assert paused.json()["rule"]["enabled"] is False

    gone = client.delete(f"/api/rules/{rule_id}", headers=H)
    assert gone.json() == {"deleted": "weekday-digest", "id": rule_id}
    assert client.get(f"/api/rules/{rule_id}", headers=H).status_code == 404
    assert client.delete(f"/api/rules/{rule_id}", headers=H).status_code == 404
    assert client.post(f"/api/rules/{rule_id}/enabled", headers=H).status_code == 404
    assert client.put(f"/api/rules/{rule_id}", headers=H, json={}).status_code == 404


def test_validating_reports_the_exact_place_a_rule_is_wrong(client):
    good = client.post("/api/rules/validate", headers=H, json={"source": WEEKDAY, "kind": "condition"})
    assert good.status_code == 200 and good.json()["ok"] is True
    bad = client.post("/api/rules/validate", headers=H, json={"source": "let a = 1;\nlet b = ;", "kind": "condition"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["line"] == 2 and bad.json()["detail"]["phase"] == "compile"
    assert bad.json()["detail"]["limit"] == "syntax"


def test_creating_a_broken_rule_is_a_422_with_a_position(client):
    bad = client.post("/api/rules", headers=H, json={"name": "broken", "source": "let a = 1;\nlet b = ;",
                                                     "kind": "condition"})
    assert bad.status_code == 422 and bad.json()["detail"]["line"] == 2
    assert client.get("/api/rules", headers=H).json()["rules"] == []


def test_testing_a_rule_runs_it_against_a_sample(client, temp_db):
    """`test` on an unsaved source, and on a saved rule with the sample the page built."""
    loose = client.post("/api/rules/test", headers=H, json={
        "source": 'ctx.message.contains("deploy")', "kind": "condition",
        "context": {"message": "please deploy", "weekday": 0, "hour": 10}})
    assert loose.status_code == 200 and loose.json()["result"] is True
    assert loose.json()["backend"] == "abp_rules" and loose.json()["elapsed_ms"] >= 0

    missed = client.post("/api/rules/test", headers=H, json={
        "source": 'ctx.message.contains("deploy")', "kind": "condition", "context": {"message": "lunch"}})
    assert missed.json()["result"] is False

    route = client.post("/api/rules/test", headers=H, json={
        "source": 'if ctx.platform == "slack" { "cli" } else { "api" }', "kind": "route",
        "context": {"platform": "slack"}})
    assert route.json()["result"] == "cli"

    rule_id = client.post("/api/rules", headers=H, json={
        "name": "evening", "source": 'ctx.hour >= 17 && ctx.message.contains("digest")',
        "kind": "condition"}).json()["rule"]["id"]
    saved = client.post(f"/api/rules/{rule_id}/test", headers=H, json={"context": {"message": "the digest", "hour": 18}})
    assert saved.status_code == 200 and saved.json()["result"] is True and saved.json()["rule"] == "evening"
    assert saved.json()["context"]["platform"] == "telegram", "the sample is filled in with real context fields"

    too_late = client.post(f"/api/rules/{rule_id}/test", headers=H, json={"context": {"message": "the digest", "hour": 9}})
    assert too_late.json()["result"] is False


def test_testing_a_rule_that_fails_says_where(client):
    loops = client.post("/api/rules/test", headers=H, json={"source": "loop { }", "kind": "condition"})
    assert loops.status_code == 422 and loops.json()["detail"]["limit"] == "operations"


def test_the_listing_can_be_narrowed(client):
    client.post("/api/rules", headers=H, json={"name": "a", "source": "true", "kind": "condition"})
    client.post("/api/rules", headers=H, json={"name": "b", "source": '"cli"', "kind": "route", "target": "backend"})
    assert [r["name"] for r in client.get("/api/rules?kind=route", headers=H).json()["rules"]] == ["b"]
    assert [r["name"] for r in client.get("/api/rules?target=backend", headers=H).json()["rules"]] == ["b"]
    client.post("/api/rules", headers=H, json={"name": "c", "source": "true", "kind": "condition",
                                               "instance_id": 1})
    assert [r["name"] for r in client.get("/api/rules?instance_id=1", headers=H).json()["rules"]] == ["a", "b", "c"]
    assert [r["name"] for r in client.get("/api/rules?instance_id=2", headers=H).json()["rules"]] == ["a", "b"]


def test_writes_are_audited(client):
    rule_id = client.post("/api/rules", headers=H, json={"name": "audited", "source": "true", "kind": "condition"}
                          ).json()["rule"]["id"]
    client.delete(f"/api/rules/{rule_id}", headers=H)
    actions = [r["action"] for r in db.get_conn().execute(
        "SELECT action FROM audit_log WHERE action LIKE 'rule_%' ORDER BY id").fetchall()]
    assert "rule_create" in actions and "rule_delete" in actions