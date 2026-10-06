"""The three places a rule is consulted, each with a rule and without one.

The claim under test is the one that matters for every other feature in ABP: **a rule that is not set
changes nothing**. So every case here is written as a pair - the same turn, the same routine, the
same hook - once with a rule bound to it and once without, and the "without" case asserts the
behaviour is exactly what it was before rules existed.

These run the real code: the real scheduler poll loop, the real hook dispatch (a real subprocess
command), and the real `Router.ask()` chain. Only the model at the end of a turn is replaced, which
is not what any of these tests is about.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

pytest.importorskip("abp_rules", reason="abp_rules is not built; run native/abp-rules/build.py --venv ...")

from bot import bot_instances, db, routines, rules, scheduler  # noqa: E402
from bot.agent_runtime import hooks  # noqa: E402
from bot.router import router  # noqa: E402

TEMPLATE = "Summarise the open pull requests in {{repo}} and list anything blocked."
PARAMS = {"repo": {"description": "owner/name"}}
HOOK_SCRIPT = Path("hook_filter_probe.py")


@pytest.fixture
def iid(temp_db):
    return bot_instances.create_instance(name="r", platform="telegram", backend="api",
                                         credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
                                         allowed_user_ids=[1])


@pytest.fixture
def probe(tmp_path):
    """A real hook: a script on disk that records the events it was given. Nothing about it is
    stubbed - it is the command an operator would actually configure."""
    script = tmp_path / HOOK_SCRIPT
    log = tmp_path / "fired.jsonl"
    script.write_text(
        "import json, os, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "with open(os.environ['ABP_RULES_PROBE_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(payload) + '\\n')\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8")
    return script, log


def fired(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- routine conditions ---------------------------------------------------------------------------
def test_a_routine_without_a_rule_runs_exactly_as_before(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    assert routines.scheduled_run_allowed(sched_id) == (True, "")


def test_a_routine_with_a_satisfied_rule_runs(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    rules.store.create("weekday-only", "ctx.weekday < 5", "condition", target="routine:digest")
    allowed, why = routines.scheduled_run_allowed(sched_id, now=dt.datetime(2026, 10, 5, 10, 0))   # a Monday
    assert allowed is True and why == ""

    blocked, why = routines.scheduled_run_allowed(sched_id, now=dt.datetime(2026, 10, 10, 10, 0))  # a Saturday
    assert blocked is False and "weekday-only" in why


def test_a_rule_can_look_at_the_prompt_the_routine_would_run(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/security"})
    rules.store.create("security-only", 'ctx.message.contains("security")', "condition", target="routine:digest")
    assert routines.scheduled_run_allowed(sched_id, prompt="Summarise o/security")[0] is True
    assert routines.scheduled_run_allowed(sched_id, prompt="Summarise o/docs")[0] is False


def test_two_conditions_on_one_routine_both_have_to_agree(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    rules.store.create("weekday", "ctx.weekday < 5", "condition", target="routine:digest")
    rules.store.create("morning", "ctx.hour < 12", "condition", target="routine:digest")
    assert routines.scheduled_run_allowed(sched_id, now=dt.datetime(2026, 10, 5, 9, 0))[0] is True
    assert routines.scheduled_run_allowed(sched_id, now=dt.datetime(2026, 10, 5, 14, 0))[0] is False


def test_a_rule_bound_to_another_routine_does_not_gate_this_one(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    rules.store.create("elsewhere", "false", "condition", target="routine:something-else")
    assert routines.scheduled_run_allowed(sched_id)[0] is True


def test_a_disabled_rule_does_not_gate_anything(iid, temp_db):
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    rule = rules.store.create("never", "false", "condition", target="routine:digest")
    rules.store.set_enabled(rule.id, False)
    assert routines.scheduled_run_allowed(sched_id)[0] is True


def test_a_rule_that_will_not_run_does_not_gate_anything_either(iid, temp_db, caplog):
    """Fail open, like every other hook failure in ABP: a broken rule is logged and ignored, because
    a typo in a rule must never be the reason a routine silently stops running forever."""
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    sched_id = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    rule = rules.store.create("broken", "true", "condition", target="routine:digest")
    db.get_conn().execute("UPDATE rules SET source=? WHERE id=?", ("loop { }", rule.id))
    db.get_conn().commit()
    with caplog.at_level("WARNING"):
        assert routines.scheduled_run_allowed(sched_id)[0] is True
    assert any("broken" in record.getMessage() for record in caplog.records)


def test_the_scheduler_asks_the_rule_before_it_dispatches(monkeypatch, temp_db, iid):
    """End to end: the real scheduler poll loop, a real scheduled row, and a rule that says no. The
    turn must not happen, and the row must be re-armed rather than left due."""
    rid = routines.save(iid, "digest", TEMPLATE, params=PARAMS)
    made = routines.schedule(routines.get_by_id(rid), "5", 3600, {"repo": "o/r"})
    sched_id = made
    rules.store.create("never", "false", "condition", target="routine:digest")
    prompts: list[str] = []

    async def fake_ask(prompt, **kwargs):
        prompts.append(prompt)
        from types import SimpleNamespace

        return SimpleNamespace(text="should never get here")

    monkeypatch.setattr("bot.router.router.ask", fake_ask)
    monkeypatch.setattr("bot.outbox.send_message", _never_sends)
    with db._lock:
        conn = db.get_conn()
        conn.execute("UPDATE scheduled_commands SET next_run_at=? WHERE id=?", (db._now(), sched_id))
        conn.commit()

    async def poll_once():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop))
        for _ in range(200):
            if db.get_scheduled_command(sched_id)["next_run_at"] != db._now():
                break
            await asyncio.sleep(0.02)
        stop.set()
        await task

    before = db.get_scheduled_command(sched_id)["next_run_at"]
    asyncio.run(poll_once())
    assert prompts == [], "a rule that said no must stop the turn before it starts"
    assert routines.history(rid) == [], "nothing ran, so the routine has no run to record"
    assert db.get_scheduled_command(sched_id)["next_run_at"] != before, "the schedule was re-armed"
    assert db.get_scheduled_command(sched_id)["enabled"] == 1

    # ...and with the rule gone the very same row runs, through the real loop.
    for rule in rules.store.listing(kind="condition"):
        rules.store.delete(rule.id)
    fired: list[str] = []

    async def recording_ask(prompt, **kwargs):
        fired.append(prompt)
        from types import SimpleNamespace

        return SimpleNamespace(text="done")

    monkeypatch.setattr("bot.router.router.ask", recording_ask)
    with db._lock:
        conn = db.get_conn()
        conn.execute("UPDATE scheduled_commands SET next_run_at=? WHERE id=?", (db._now(), sched_id))
        conn.commit()

    async def poll_again():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop))
        for _ in range(300):
            if routines.history(rid):
                break
            await asyncio.sleep(0.02)
        stop.set()
        await task

    asyncio.run(poll_again())
    assert fired and "o/r" in fired[0]
    assert routines.history(rid)[0]["outcome"] == "ok"


async def _never_sends(*a, **kw):
    """No live platform in a test: the turn is the point, not the delivery."""
    return None


# ---- hook filters ----------------------------------------------------------------------------------
def _add_hook(event: str, command: str, matcher=None, instance_id=None) -> int:
    return db.add_agent_hook(event=event, matcher=matcher, command=command, instance_id=instance_id)


def test_a_hook_without_a_rule_runs_exactly_as_before(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert [event["event"] for event in fired(log)] == ["PreToolUse"]


def test_a_hook_filter_that_says_yes_lets_the_hook_run(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"', matcher="Bash")
    rules.store.create("bash-only", 'ctx.tool == "Bash"', "filter", target="hook:PreToolUse:Bash")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert len(fired(log)) == 1


def test_a_hook_filter_that_says_no_stops_the_hook(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"', matcher="Bash")
    rules.store.create("never-bash", 'ctx.tool != "Bash"', "filter", target="hook:PreToolUse:Bash")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert fired(log) == [], "the hook ran even though its filter said no"
    # ...and deleting the one rule is what lets the very same hook through, which is what shows the
    # filter was the thing that stopped it rather than the hook failing to match.
    rules.store.delete(rules.store.get_by_name("never-bash").id)
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert len(fired(log)) == 1


def test_a_filter_narrowed_to_an_event_gates_every_tool_on_it(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    rules.store.create("no-bash-anywhere", 'ctx.tool != "Bash"', "filter", target="hook:PreToolUse")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert fired(log) == []
    asyncio.run(hooks.run_pre_tool_use("Read", {"path": "x"}, instance_id=iid))
    assert len(fired(log)) == 1


def test_two_filters_both_have_to_agree(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    rules.store.create("one", 'ctx.tool == "Bash"', "filter", target="hook:PreToolUse")
    rules.store.create("two", "false", "filter", target="hook:PreToolUse")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert fired(log) == []


def test_a_hook_filter_on_another_event_does_not_gate_this_one(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    rules.store.create("elsewhere", "false", "filter", target="hook:PostToolUse")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert len(fired(log)) == 1


def test_a_hook_filter_can_read_the_payload(monkeypatch, temp_db, iid, probe):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    rules.store.create("no-rm", '!ctx.tool_input.command.contains("rm -rf")', "filter", target="hook:PreToolUse")
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls -la"}, instance_id=iid))
    assert len(fired(log)) == 1
    asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "rm -rf /"}, instance_id=iid))
    assert len(fired(log)) == 1, "the dangerous command's hook must not run"


def test_a_filter_can_stop_a_deny_hook_from_denying(monkeypatch, temp_db, iid, probe, tmp_path):
    """The filter runs before the hook, so a hook's "deny" cannot be reached - which is the whole
    point of gating a permission hook."""
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    deny = tmp_path / "deny.py"
    deny.write_text(
        "import json, sys\njson.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'decision': 'deny', 'reason': 'never'}))\n", encoding="utf-8")
    _add_hook("PreToolUse", f'"{deny.as_posix()}"', matcher="Bash")
    assert asyncio.run(hooks.run_pre_tool_use("Bash", {}, instance_id=iid))[0] == "deny"

    rules.store.create("allow-it", "false", "filter", target="hook:PreToolUse:Bash")
    assert asyncio.run(hooks.run_pre_tool_use("Bash", {}, instance_id=iid))[0] == "allow"


def test_a_hook_filter_that_will_not_run_lets_the_hook_run(monkeypatch, temp_db, iid, probe, caplog):
    script, log = probe
    monkeypatch.setenv("ABP_RULES_PROBE_LOG", str(log))
    _add_hook("PreToolUse", f'"{Path.cwd() / script.as_posix()}"')
    rule = rules.store.create("broken", "true", "filter", target="hook:PreToolUse")
    db.get_conn().execute("UPDATE rules SET source=? WHERE id=?", ("loop { }", rule.id))
    db.get_conn().commit()
    with caplog.at_level("WARNING"):
        asyncio.run(hooks.run_pre_tool_use("Bash", {"command": "ls"}, instance_id=iid))
    assert len(fired(log)) == 1, "a broken filter must not take the hook down with it"


# ---- routing ---------------------------------------------------------------------------------------
def test_no_route_rule_means_the_chain_is_exactly_what_it_was(iid, temp_db):
    chain = router.resolve_chain("quick_question", None, instance_id=iid)
    assert router._rule_route("hello", "quick_question", iid, 1) == (None, None)
    assert router.resolve_chain("quick_question", None, instance_id=iid) == chain


def test_a_route_rule_picks_the_backend_and_leaves_the_backups_behind(iid, temp_db):
    rules.store.create("cli-for-images", 'if ctx.message.contains("image") { "cli" } else { "" }',
                       "route", target="backend")
    picked, model = router._rule_route("an image please", "quick_question", iid, 1)
    assert picked == "cli" and model is None
    assert router._rule_route("just a question", "quick_question", iid, 1) == (None, None)


def test_a_route_rule_picks_the_model(iid, temp_db):
    rules.store.create("big-model-for-code", 'if ctx.message.contains("refactor") { "some-large-model" } else { "" }',
                       "route", target="model")
    backend, model = router._rule_route("please refactor this", "quick_question", iid, 1)
    assert model == "some-large-model" and backend is None
    assert router._rule_route("hello", "quick_question", iid, 1) == (None, None)


def test_a_route_rule_naming_something_that_is_not_a_backend_is_ignored(iid, temp_db, caplog):
    rules.store.create("nonsense", '"not-a-backend"', "route", target="backend")
    with caplog.at_level("WARNING"):
        assert router._rule_route("hello", "quick_question", iid, 1) == (None, None)
    assert any("not a backend" in r.getMessage() for r in caplog.records)


def test_the_router_actually_uses_the_rule(monkeypatch, temp_db, iid):
    """The whole path: `Router.ask()` with the real chain resolution, the rule consulted, the chosen
    backend asked, and the chosen model handed to it. Only the backend at the end is replaced."""
    asked: list[tuple[str, dict]] = []
    models: list = []

    class _Recorder:
        name = "api"
        model = None

        async def ask(self, prompt, *, context=None, timeout_s=30):
            from bot.backends.base import BackendResult

            asked.append((prompt, dict(context or {})))
            return BackendResult(text="answered by the recorded backend")

    def _record_backend(*args, **kwargs):
        # What the backend was actually built with: the model a route rule named has to arrive here,
        # or the rule only decided something in a log line.
        models.append(kwargs.get("model_override"))
        return _Recorder()

    monkeypatch.setattr(router, "_get_backend", _record_backend)
    monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))
    monkeypatch.setattr("bot.privacy.enabled", lambda: False)

    rules.store.create("always-cli", '"cli"', "route", target="backend")
    rules.store.create("always-model", '"pinned-model"', "route", target="model")
    result = asyncio.run(router.ask("hello there", action_type="quick_question", user_id=1, instance_id=iid))
    assert result.text == "answered by the recorded backend"
    row = db.get_latest_job(iid, "quick_question")
    assert row["backend"] == "cli", "the job row records the backend the rule chose"
    assert models[-1] == "pinned-model", "the backend was built with the model the rule chose"

    # ...and a turn with no instance of its own is still routed by the same rules.
    asyncio.run(router.ask("and this one", action_type="quick_question", user_id=1))
    assert models[-1] == "pinned-model" and asked[-1][0] == "and this one"

    # ...and with no rules at all the very same call routes exactly as before.
    for rule in rules.store.listing(kind="route"):
        rules.store.delete(rule.id)
    asked.clear()
    assert asyncio.run(router.ask("hello again", action_type="quick_question", user_id=1, instance_id=iid)).text
    assert db.get_latest_job(iid, "quick_question")["backend"] == "api"
    assert models[-1] is None, "with no rule the instance's own model (none here) is what the backend gets"


def test_an_explicit_backend_flag_beats_a_route_rule(monkeypatch, temp_db, iid):
    """A flag a person typed on the command line cannot be overruled by configuration, or the flag
    would be a lie."""
    rules.store.create("always-cli", '"cli"', "route", target="backend")
    assert router._rule_route("hello", "quick_question", iid, 1) == ("cli", None)
    assert router.resolve_chain("quick_question", "api", instance_id=iid) == ["api"]


def test_a_route_rule_scoped_to_one_bot_leaves_the_others_alone(iid, temp_db):
    other = bot_instances.create_instance(name="other", platform="telegram", backend="api",
                                          credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[2])
    rules.store.create("only-this-one", '"cli"', "route", target="backend", instance_id=iid)
    assert router._rule_route("hello", "quick_question", iid, 1)[0] == "cli"
    assert router._rule_route("hello", "quick_question", other, 1)[0] is None

