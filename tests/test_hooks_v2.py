"""Hooks v2: more events, input rewriting, HTTP hooks, Stop (roadmap P2)."""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import httpx
import pytest

from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say
from bot import db
from bot.agent_runtime import hooks, permissions, tool_loop, tools
from bot.agent_runtime.transports.base import NormalizedResponse
from bot.backends.native_backend import NativeAgentBackend

REAL_CLIENT = httpx.AsyncClient


def run(coro):
    return asyncio.run(coro)


def script(tmp_path, body, name="hook.py"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return f'"{sys.executable}" "{path}"'


def add(event, command, matcher=None):
    db.add_agent_hook(event, command, matcher=matcher)


# ---- events ----------------------------------------------------------------------
def test_all_ten_events_are_valid():
    assert hooks.VALID_EVENTS == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd",
                                  "UserPromptSubmit", "Stop", "SubagentStop", "PreCompact", "Notification"}


# ---- PreToolUse: rewriting and precedence -------------------------------------------
def test_a_hook_can_rewrite_the_tool_input(temp_db, tmp_path):
    add("PreToolUse", script(tmp_path, """
        import json, sys
        p = json.load(sys.stdin)
        print(json.dumps({"updatedInput": {**p["tool_input"], "command": "echo rewritten"}}))
    """), matcher="run_shell")
    result = run(hooks.run_pre_tool_use_full("run_shell", {"command": "echo original"}))
    assert result.decision == "allow" and result.updated_input == {"command": "echo rewritten"}
    assert run(hooks.run_pre_tool_use("run_shell", {"command": "x"})) == ("allow", None)


def test_a_deny_beats_a_rewrite_from_an_earlier_hook(temp_db, tmp_path):
    add("PreToolUse", script(tmp_path, 'import json; print(json.dumps({"updatedInput": {"command": "x"}}))', "a.py"), "run_shell")
    add("PreToolUse", script(tmp_path, 'import json; print(json.dumps({"decision": "deny", "reason": "no"}))', "b.py"), "run_shell")
    result = run(hooks.run_pre_tool_use_full("run_shell", {"command": "y"}))
    assert result.decision == "deny" and result.reason == "no"


def test_a_non_object_updated_input_is_ignored(temp_db, tmp_path):
    add("PreToolUse", script(tmp_path, 'import json; print(json.dumps({"updatedInput": "oops"}))'), "run_shell")
    assert run(hooks.run_pre_tool_use_full("run_shell", {"command": "y"})).updated_input is None


class NoApproval:
    async def request_approval(self, *a, **k):
        return "deny"


def go(ws, name, inp):
    return asyncio.run(tool_loop.run_one_tool(
        name, inp, workspace=ws, instance_id=None, chat_id=1, session_key="s", notify=None, agent_tools=tools,
        agent_approval=NoApproval()))


def test_a_rewritten_call_is_judged_again_by_the_permission_rules(temp_db, tmp_path, monkeypatch):
    ws = (tmp_path / "ws")
    ws.mkdir()
    monkeypatch.setattr(tool_loop, "try_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(permissions, "_config", lambda: {"rules": [
        {"decision": "allow", "tool": "run_shell", "match": "echo *"}, {"decision": "deny", "tool": "run_shell", "match": "rm *"}]})
    add("PreToolUse", script(tmp_path, 'import json; print(json.dumps({"updatedInput": {"command": "rm -rf ."}}))'), "run_shell")
    out = go(ws.resolve(), "run_shell", {"command": "echo hi"})
    assert out.startswith("Denied by policy") and "rm" in out


def test_a_rewrite_is_what_actually_runs(temp_db, tmp_path, monkeypatch):
    ws = (tmp_path / "ws")
    ws.mkdir()
    monkeypatch.setattr(tool_loop, "try_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(permissions, "_config", lambda: {"rules": [{"decision": "allow", "tool": "run_shell", "match": "echo *"}]})
    add("PreToolUse", script(tmp_path, 'import json; print(json.dumps({"updatedInput": {"command": "echo rewritten"}}))'), "run_shell")
    assert "rewritten" in go(ws.resolve(), "run_shell", {"command": "echo original"})


# ---- HTTP hooks ---------------------------------------------------------------------
def install_http(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_an_http_hook_receives_the_event_and_answers_json(temp_db, monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"decision": "deny", "reason": "blocked by policy service"})

    install_http(monkeypatch, handler)
    add("PreToolUse", "http://127.0.0.1:9000/hook", "run_shell")
    result = run(hooks.run_pre_tool_use_full("run_shell", {"command": "ls"}))
    assert result.decision == "deny" and result.reason == "blocked by policy service"
    assert seen["body"]["event"] == "PreToolUse" and seen["body"]["tool_name"] == "run_shell"


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(200, text="not json"),
                                      httpx.Response(200, json=["a list"]), httpx.Response(204)])
def test_a_failing_or_odd_http_hook_is_no_opinion(temp_db, monkeypatch, response):
    install_http(monkeypatch, lambda r: response)
    add("PreToolUse", "https://hooks.example/pre", "run_shell")
    assert run(hooks.run_pre_tool_use("run_shell", {})) == ("allow", None)


def test_an_unreachable_http_hook_is_no_opinion(temp_db, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    install_http(monkeypatch, handler)
    add("PreToolUse", "https://hooks.example/pre", "run_shell")
    assert run(hooks.run_pre_tool_use("run_shell", {})) == ("allow", None)


# ---- the other events -----------------------------------------------------------------
def marker_hook(tmp_path, name, extra_output=""):
    log = tmp_path / f"{name}.log"
    body = f"""
        import json, sys
        p = json.load(sys.stdin)
        open(r"{log}", "a").write(json.dumps(p) + "\\n")
        {extra_output}
    """
    return script(tmp_path, body, f"{name}.py"), log


def test_a_failing_tool_fires_post_tool_use_failure(temp_db, tmp_path, monkeypatch):
    cmd, log = marker_hook(tmp_path, "fail")
    add("PostToolUseFailure", cmd)
    ws = (tmp_path / "ws")
    ws.mkdir()
    out = go(ws.resolve(), "read_file", {"path": "missing.txt"})
    assert out.startswith("Error:")
    payload = json.loads(log.read_text().splitlines()[0])
    assert payload["event"] == "PostToolUseFailure" and payload["tool_name"] == "read_file" and "does not exist" in payload["error"]


def test_notification_and_session_end_and_subagent_stop_hooks_receive_their_payloads(temp_db, tmp_path):
    for event, fn in (("Notification", lambda: hooks.run_notification("permission_request", "run_shell is waiting")),
                      ("SessionEnd", lambda: hooks.run_session_end(reason="new_session")),
                      ("SubagentStop", lambda: hooks.run_subagent_stop("child finished"))):
        cmd, log = marker_hook(tmp_path, event)
        add(event, cmd)
        run(fn())
        assert json.loads(log.read_text().splitlines()[0])["event"] == event


def test_pre_compact_context_reaches_the_summary_prompt(temp_db, tmp_path, monkeypatch):
    from bot.agent_runtime import compression

    add("PreCompact", script(tmp_path, 'import json; print(json.dumps({"additionalContext": "keep every file path"}))'))
    monkeypatch.setattr(compression, "threshold_chars", lambda: 10)
    sent = {}

    class T:
        def user_message(self, text):
            return {"role": "user", "content": text}

        async def send(self, **kw):
            sent["prompt"] = kw["history"][0]["content"]
            return NormalizedResponse(text="summary", assistant_message={"role": "assistant", "content": "summary"})

    for i in range(12):
        db.append_agent_message("compact-session", "user" if i % 2 == 0 else "assistant", "message number %d " % i * 5)
    assert run(compression.maybe_compress("compact-session", T(), model="m", instance_id=None)) is True
    assert "Also: keep every file path" in sent["prompt"]


# ---- Stop -----------------------------------------------------------------------------
def test_a_stop_hook_can_make_the_agent_keep_going_but_only_twice(temp_db, tmp_path):
    cmd, log = marker_hook(tmp_path, "stop", 'print(json.dumps({"decision": "block", "reason": "run the tests first"}))')
    add("Stop", cmd)
    t = ScriptedTransport([Say("done"), Say("done again"), Say("still done"), Say("never reached")])
    result = run(NativeAgentBackend(t, model="m").ask("go", context={"cwd": str(tmp_path)}))
    assert result.text == "still done" and t.calls == 3
    assert len(log.read_text().splitlines()) == 2


def test_a_stop_hook_that_allows_changes_nothing(temp_db, tmp_path):
    add("Stop", script(tmp_path, 'import json; print(json.dumps({}))'))
    t = ScriptedTransport([Say("done")])
    assert run(NativeAgentBackend(t, model="m").ask("go", context={"cwd": str(tmp_path)})).text == "done"
    assert t.calls == 1
