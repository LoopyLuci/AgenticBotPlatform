"""bot/agent_runtime/hooks.py — Claude Code-style lifecycle hooks (Phase E
of the Claude API/Claude Code parity plan). Uses real subprocess hook
commands (a small Python script read from stdin, written to stdout) —
the same "spawn a real shell command" mechanism bot/agent_runtime/tools.py's
run_shell already exercises in this environment, not a mocked subprocess.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

from bot import db
from bot.agent_runtime import hooks


def _run(coro):
    return asyncio.run(coro)


def _hook_script(tmp_path, body: str) -> str:
    """Writes a small Python hook script reading JSON from stdin and
    printing JSON to stdout, then returns the shell command to run it —
    real subprocess execution, matching this codebase's own run_shell
    testing convention."""
    script = tmp_path / f"hook_{abs(hash(body)) % 100000}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


ECHO_ALLOW = """
    import json, sys
    json.dump({"decision": "allow"}, sys.stdout)
"""

ECHO_DENY = """
    import json, sys
    payload = json.load(sys.stdin)
    json.dump({"decision": "deny", "reason": f"blocked {payload['tool_name']}"}, sys.stdout)
"""

ECHO_ASK = """
    import json, sys
    json.dump({"decision": "ask"}, sys.stdout)
"""

ECHO_CONTEXT = """
    import json, sys
    json.dump({"additionalContext": "extra context from a hook"}, sys.stdout)
"""

NON_JSON_OUTPUT = """
    print("not json at all")
"""

SLOW_HOOK = """
    import time
    time.sleep(60)
"""


def test_pre_tool_use_allows_when_no_hook_configured(temp_db):
    decision, reason = _run(hooks.run_pre_tool_use("run_shell", {"command": "ls"}))
    assert decision == "allow"
    assert reason is None


def test_pre_tool_use_deny(temp_db, tmp_path):
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_DENY), matcher="run_shell")

    decision, reason = _run(hooks.run_pre_tool_use("run_shell", {"command": "ls"}))

    assert decision == "deny"
    assert reason == "blocked run_shell"


def test_pre_tool_use_ask(temp_db, tmp_path):
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_ASK), matcher="run_shell")

    decision, reason = _run(hooks.run_pre_tool_use("run_shell", {"command": "ls"}))

    assert decision == "ask"


def test_pre_tool_use_matcher_only_fires_for_the_named_tool(temp_db, tmp_path):
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_DENY), matcher="write_file")

    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {"command": "ls"}))

    assert decision == "allow"


def test_pre_tool_use_empty_matcher_fires_for_every_tool(temp_db, tmp_path):
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_ASK))

    decision, _ = _run(hooks.run_pre_tool_use("any_tool_name", {}))

    assert decision == "ask"


def test_disabled_hook_never_fires(temp_db, tmp_path):
    hook_id = db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_DENY))
    db.set_agent_hook_enabled(hook_id, False)

    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {}))

    assert decision == "allow"


def test_non_json_hook_output_is_treated_as_no_opinion(temp_db, tmp_path):
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, NON_JSON_OUTPUT))

    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {}))

    assert decision == "allow"


def test_timed_out_hook_is_treated_as_no_opinion(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_S", 0.2)
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, SLOW_HOOK))

    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {}))

    assert decision == "allow"


def test_post_tool_use_never_raises_even_with_no_hook(temp_db):
    _run(hooks.run_post_tool_use("run_shell", {"command": "ls"}, "output text"))


def test_post_tool_use_hook_actually_receives_the_output(temp_db, tmp_path):
    captured_file = tmp_path / "captured.json"
    body = f"""
    import json, sys
    payload = json.load(sys.stdin)
    with open(r"{captured_file}", "w") as f:
        json.dump(payload, f)
    """
    db.add_agent_hook("PostToolUse", _hook_script(tmp_path, body), matcher="run_shell")

    _run(hooks.run_post_tool_use("run_shell", {"command": "ls"}, "the real output"))

    captured = json.loads(captured_file.read_text())
    assert captured["output"] == "the real output"
    assert captured["tool_name"] == "run_shell"


def test_session_start_returns_none_with_no_hook(temp_db):
    assert _run(hooks.run_session_start()) is None


def test_session_start_returns_additional_context(temp_db, tmp_path):
    db.add_agent_hook("SessionStart", _hook_script(tmp_path, ECHO_CONTEXT))

    result = _run(hooks.run_session_start())

    assert result == "extra context from a hook"


def test_user_prompt_submit_returns_additional_context(temp_db, tmp_path):
    db.add_agent_hook("UserPromptSubmit", _hook_script(tmp_path, ECHO_CONTEXT))

    result = _run(hooks.run_user_prompt_submit("hello"))

    assert result == "extra context from a hook"


def test_multiple_context_hooks_are_joined(temp_db, tmp_path):
    db.add_agent_hook("SessionStart", _hook_script(tmp_path, ECHO_CONTEXT))
    body2 = """
    import json, sys
    json.dump({"additionalContext": "second hook's context"}, sys.stdout)
    """
    db.add_agent_hook("SessionStart", _hook_script(tmp_path, body2))

    result = _run(hooks.run_session_start())

    assert "extra context from a hook" in result
    assert "second hook's context" in result


def test_instance_scoping(temp_db, tmp_path):
    from bot import bot_instances

    iid = bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, ECHO_DENY), matcher="run_shell", instance_id=iid)

    # A global (no instance_id given) call never sees an instance-scoped hook.
    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {}))
    assert decision == "allow"

    # The scoped instance does see it.
    decision, _ = _run(hooks.run_pre_tool_use("run_shell", {}, instance_id=iid))
    assert decision == "deny"


# --------------------------------------------------------------- db CRUD --

def test_db_add_list_enable_delete_round_trip(temp_db):
    hook_id = db.add_agent_hook("PreToolUse", "echo hi", matcher="run_shell")
    row = db.get_agent_hook(hook_id)
    assert row["event"] == "PreToolUse"
    assert row["matcher"] == "run_shell"
    assert bool(row["enabled"]) is True

    db.set_agent_hook_enabled(hook_id, False)
    assert bool(db.get_agent_hook(hook_id)["enabled"]) is False

    assert len(db.list_agent_hooks()) == 1
    assert db.delete_agent_hook(hook_id) is True
    assert db.get_agent_hook(hook_id) is None
    assert db.delete_agent_hook(hook_id) is False


def test_db_list_filters_by_event(temp_db):
    db.add_agent_hook("PreToolUse", "echo 1")
    db.add_agent_hook("PostToolUse", "echo 2")

    assert len(db.list_agent_hooks(event="PreToolUse")) == 1
    assert len(db.list_agent_hooks(event="PostToolUse")) == 1
    assert len(db.list_agent_hooks()) == 2
