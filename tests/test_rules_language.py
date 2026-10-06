"""The rule language itself: what a rule can do, what it cannot, and where it says so.

These run against the real `abp_rules` extension (native/abp-rules), not a stand-in: the sandbox is
the thing under test, and a fake one would prove nothing. When the extension is not built the module
skips, and tests/test_rules_without_engine.py covers the other half of the contract - that ABP is
unchanged when it is not there.

No rule here reaches the filesystem or the network, because it cannot: the sandbox registers no
function that could, and Rhai itself has none. The tests assert that rather than trust it, including
that a rule cannot even name one.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

abp_rules = pytest.importorskip("abp_rules", reason="abp_rules is not built; run native/abp-rules/build.py --venv ...")

from bot import rules  # noqa: E402  (after the importorskip, so the skip reads first)

MONDAY_MORNING = dt.datetime(2026, 10, 5, 10, 30)          # a Monday
SATURDAY_NIGHT = dt.datetime(2026, 10, 10, 23, 30)         # a Saturday


def ctx(**kwargs) -> dict:
    base = rules.message_context("deploy the release", sender="alex", sender_id=7, platform="telegram",
                                 instance_id=1, bot="assistant", tags=["ops", "release"], now=MONDAY_MORNING)
    base.update(kwargs)
    return base


# ---- the language ----------------------------------------------------------------------------------
def test_a_condition_reads_the_context():
    source = 'ctx.message.contains("deploy") && ctx.weekday < 5 && ctx.hour >= 9'
    assert rules.validate(source, "condition")["ok"] is True
    assert rules.matches(source, ctx()) is True
    assert rules.matches(source, ctx(message="what is for lunch")) is False
    assert rules.matches(source, ctx(hour=7)) is False


def test_a_top_level_key_is_the_same_value_as_ctx():
    assert rules.matches("message == ctx.message && platform == ctx.platform", ctx()) is True


def test_a_route_answers_a_value_not_a_yes_or_no():
    source = 'if ctx.tags.contains("release") { "vision" } else { "api" }'
    assert rules.evaluate(source, "route", ctx()) == "vision"
    assert rules.evaluate(source, "route", ctx(tags=["chatter"])) == "api"


def test_a_filter_answers_the_same_way_a_condition_does():
    assert rules.matches('ctx.tool == "Bash"', rules.event_context("PreToolUse", {"tool": "Bash"})) is True
    assert rules.matches('ctx.tool == "Bash"', rules.event_context("PreToolUse", {"tool": "Read"})) is False


def test_a_rule_is_compiled_once_and_reused():
    """A rule is consulted on every message it touches, so re-parsing it each time would be the whole
    cost of having one. Two caches stand between a message and a parse: the compiled program in this
    process, and the parsed AST in the engine."""
    source = 'ctx.message.contains("deploy")'
    first = rules.engine.program(source, "condition")
    misses_before = abp_rules.cache_stats()[1]
    for _ in range(20):
        assert rules.engine.program(source, "condition") is first
        assert rules.matches(source, ctx()) is True
    assert abp_rules.cache_stats()[1] == misses_before, "the rule was re-parsed on every call"


# ---- errors carry their positions -------------------------------------------------------------------
def test_a_syntax_error_names_the_line_and_the_column():
    with pytest.raises(rules.RuleError) as caught:
        rules.validate("let a = 1;\nlet b = ;\ntrue", "condition")
    problem = caught.value
    assert problem.phase == "compile" and problem.line == 2 and problem.column > 0
    assert problem.as_dict() == {"message": str(problem), "phase": "compile", "line": 2,
                                 "column": problem.column, "limit": "syntax"}
    assert problem.where == f"line 2, column {problem.column}"


def test_a_runtime_error_names_the_line_too():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate("let a = 1;\nlet b = 2;\ndefinitely_not_a_function();\ntrue", "condition", ctx())
    problem = caught.value
    assert problem.phase == "run" and problem.line == 3
    assert problem.limit == "unknown_function"


def test_a_condition_that_answers_something_else_is_refused_by_name():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate('"just a string"', "condition", ctx())
    assert "true or false" in str(caught.value) and caught.value.limit == "wrong_type"


def test_an_unknown_kind_is_refused():
    with pytest.raises(rules.RuleError):
        rules.validate("true", "spell")


def test_an_empty_rule_is_refused():
    with pytest.raises(rules.RuleError):
        rules.validate("   \n ", "condition")


# ---- the limits -------------------------------------------------------------------------------------
def test_an_infinite_loop_stops_instead_of_hanging_the_turn():
    started = dt.datetime.now()
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate("loop { }", "condition", ctx())
    assert caught.value.limit == "operations"
    assert (dt.datetime.now() - started).total_seconds() < 5


def test_a_runaway_loop_is_stopped_too():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate("let n = 0; while n < 100000000 { n += 1 } n", "condition", ctx())
    assert caught.value.limit == "operations"


def test_a_rule_cannot_build_an_enormous_string():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate('let s = "x"; let i = 0; while i < 40 { s += s; i += 1 } s', "condition", ctx())
    assert caught.value.limit == "size"


def test_an_oversized_context_string_is_refused():
    with pytest.raises(rules.RuleError) as caught:
        rules.matches("true", {"message": "x" * (abp_rules.limits()["max_string_size"] + 1)})
    assert caught.value.limit == "size"


def test_an_oversized_context_array_is_refused():
    limit = abp_rules.limits()["max_array_size"]
    with pytest.raises(rules.RuleError) as caught:
        rules.matches("true", {"tags": ["t"] * (limit + 1)})
    assert caught.value.limit == "size"


def test_a_context_that_nests_too_deeply_is_refused():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": "too far"}}}}}}}}}
    with pytest.raises(rules.RuleError) as caught:
        rules.matches("true", deep)
    assert caught.value.limit == "size"


def test_recursion_stops_at_the_call_depth_limit():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate("fn down(n) { down(n + 1) } down(0)", "route", ctx())
    assert caught.value.limit in ("call_depth", "operations")


def test_the_limits_are_reported_and_are_the_ones_in_force():
    reported = rules.limits()
    assert reported == abp_rules.limits()
    assert reported["max_operations"] > 0 and reported["max_call_levels"] > 0
    assert rules.status()["limits"] == reported


# ---- no filesystem, no network, no processes ----------------------------------------------------------
@pytest.mark.parametrize("name", ["open", "read_file", "read_to_string", "file_system", "load_module"])
def test_a_rule_cannot_reach_the_filesystem(name):
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate(f'{name}("/etc/passwd")', "route", ctx())
    assert caught.value.limit in ("unknown_function", "unknown_module", "syntax")


@pytest.mark.parametrize("name", ["http_get", "fetch", "socket", "shell", "system", "exec", "popen", "spawn_cmd"])
def test_a_rule_cannot_reach_the_network_or_a_process(name):
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate(f'{name}("http://127.0.0.1/")', "route", ctx())
    assert caught.value.limit in ("unknown_function", "unknown_module", "syntax")


def test_a_rule_cannot_import_a_module():
    with pytest.raises(rules.RuleError) as caught:
        rules.evaluate('import "std" as std; 1', "route", ctx())
    assert caught.value.limit in ("unknown_module", "unknown_function", "syntax")


def test_a_rule_cannot_write_to_stdout(capsys):
    with pytest.raises(rules.RuleError):
        rules.evaluate('print("this must never appear anywhere")', "route", ctx())
    assert "this must never appear anywhere" not in capsys.readouterr().out


def test_a_rule_cannot_reach_a_python_object():
    """There is no bridge out of the sandbox at all: whatever Python put in the context is plain
    data, so a rule cannot walk from a string to the process that owns it."""
    with pytest.raises(rules.RuleError) as caught:
        rules.matches("true", {"handle": os.getcwd()})     # a plain string, which is fine
        rules.matches("true", {"handle": object()})       # not plain data, so refused by name
    assert "rule context may hold only" in str(caught.value)


# ---- what a rule sees ------------------------------------------------------------------------------
def test_the_context_is_exactly_what_the_context_module_says_it_is():
    built = rules.message_context("hello there", sender="sam", sender_id=3, platform="discord",
                                  action_type="quick_question", instance_id=4, bot="helper",
                                  tags=["x"], now=MONDAY_MORNING)
    assert set(built) == {"message", "sender", "sender_id", "platform", "action_type", "instance_id", "bot",
                          "tags", "hour", "minute", "weekday", "weekend", "date", "time", "timestamp"}
    assert built["hour"] == 10 and built["weekday"] == 0 and built["date"] == "2026-10-05"
    assert built["time"] == "10:30" and built["weekend"] is False


def test_extra_fields_reach_a_rule():
    built = rules.build_context(message="x", extra={"tool": "Bash", "event": "PreToolUse"})
    assert rules.matches('ctx.tool == "Bash" && ctx.event == "PreToolUse"', built) is True


def test_a_context_key_that_is_not_a_name_is_refused_by_name():
    with pytest.raises(rules.RuleError) as caught:
        rules.matches("true", {"not a name": 1})
    assert "not a name" in str(caught.value)


def test_a_pasted_log_does_not_make_the_context_unusable():
    """A 40k-character paste is trimmed rather than refused: the rule still sees the beginning of
    the message, which is the part a routing rule is about."""
    built = rules.message_context("y" * 40_000)
    assert rules.matches("ctx.message.len() > 1000", built) is True

