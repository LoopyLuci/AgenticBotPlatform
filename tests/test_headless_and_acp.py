"""Headless runs (abp_run) and the Agent Client Protocol server (abp_acp) - roadmap P5."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from abp_acp.server import AcpServer, prompt_text
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say
from abp_run import __main__ as run_cli
from abp_run import core

ROOT = Path(__file__).resolve().parent.parent


def script(*steps):
    return ScriptedTransport(list(steps))


# ---- abp_run ------------------------------------------------------------------------------------------
def test_a_headless_run_answers_and_leaves_nothing_behind(tmp_path):
    (tmp_path / "config.json").write_text('{"port": 8123}')
    real_db = core.__dict__  # noqa: F841 - the real database must not be touched: proven below by the path check
    from bot import db as db_module

    before = db_module.DB_PATH
    result = core.run_once("what port?", provider="anthropic", model="m", cwd=tmp_path,
                           transport=script(Call("read_file", {"path": "config.json"}), Say("8123")))
    assert result.ok and result.reply == "8123" and result.exit_code == 0
    assert [c["tool"] for c in result.tool_calls] == ["read_file"] and result.iterations == 2 and result.run_id
    assert db_module.DB_PATH == before


def test_approval_defaults_to_deny_and_allow_is_opt_in(tmp_path):
    steps = lambda: script(Call("write_file", {"path": "x.txt", "content": "hi"}), Say("done"))     # noqa: E731
    denied = core.run_once("write it", provider="anthropic", model="m", cwd=tmp_path, transport=steps())
    assert denied.ok and denied.denied == 1 and not (tmp_path / "x.txt").exists()
    allowed = core.run_once("write it", provider="anthropic", model="m", cwd=tmp_path, approve="allow", transport=steps())
    assert allowed.denied == 0 and (tmp_path / "x.txt").read_text() == "hi"


def test_plan_mode_is_read_only_even_when_approvals_are_allowed(tmp_path):
    r = core.run_once("write it", provider="anthropic", model="m", cwd=tmp_path, approve="allow", permission_mode="plan",
                      transport=script(Call("write_file", {"path": "x.txt", "content": "hi"}), Say("done")))
    assert not (tmp_path / "x.txt").exists() and r.denied == 1


def test_a_failing_model_is_a_result_with_a_distinct_exit_code(tmp_path):
    from bot.backends.base import BackendError

    class Boom(ScriptedTransport):
        async def send(self, **kw):
            raise BackendError("boom")

    class Limited(ScriptedTransport):
        async def send(self, **kw):
            raise BackendError("returned 429: slow down")

    a = core.run_once("x", provider="anthropic", model="m", cwd=tmp_path, transport=Boom([]))
    assert not a.ok and a.error == "boom" and a.exit_code == core.EXIT_FAILED
    b = core.run_once("x", provider="anthropic", model="m", cwd=tmp_path, transport=Limited([]))
    assert b.exit_code == core.EXIT_RATE


def test_hitting_a_step_limit_is_exit_code_3(tmp_path, monkeypatch):
    from bot.agent_runtime import loop_guard

    monkeypatch.setattr(loop_guard, "limits", lambda: loop_guard.Limits(max_iterations=1, max_seconds=0, max_tokens=0)
                        if hasattr(loop_guard, "Limits") else None)
    steps = [Call("list_dir", {"path": "."}) for _ in range(3)] + [Say("done")]
    r = core.run_once("loop", provider="anthropic", model="m", cwd=tmp_path, transport=script(*steps))
    assert r.ok and r.stopped and r.exit_code == core.EXIT_LIMITED


def test_the_model_reference_is_split_at_a_known_provider(monkeypatch):
    monkeypatch.setattr(core, "_providers", lambda: {"openrouter"})
    assert core.split_model("openrouter/qwen/qwen3.8-27b:free") == ("openrouter", "qwen/qwen3.8-27b:free")
    assert core.split_model("anthropic/claude-sonnet-5") == ("anthropic", "claude-sonnet-5")
    assert core.split_model("claude-sonnet-5") == ("anthropic", "claude-sonnet-5")
    assert core.split_model("unknownvendor/model") == ("anthropic", "unknownvendor/model")
    with pytest.raises(core.RunError):
        core.split_model("")
    with pytest.raises(core.RunError):
        core.transport_for("nonesuch", "m")


def test_the_command_line_json_and_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "transport_for", lambda p, m: script(Say("hello")))
    assert run_cli.main(["say hi", "--model", "anthropic/m", "--cwd", str(tmp_path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["reply"] == "hello" and out["exit_code"] == 0
    assert run_cli.main(["say hi", "--model", "anthropic/m", "--cwd", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "hello"
    assert run_cli.main(["say hi", "--cwd", str(tmp_path)]) == core.EXIT_USAGE                  # no model
    assert run_cli.main(["", "--model", "anthropic/m"]) == core.EXIT_USAGE                       # empty prompt
    assert run_cli.main(["x", "--model", "anthropic/m", "--cwd", str(tmp_path / "missing")]) == core.EXIT_USAGE


def test_the_prompt_can_come_from_standard_input(tmp_path, monkeypatch, capsys):
    import io

    monkeypatch.setattr(core, "transport_for", lambda p, m: script(Say("piped")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("do it\n"))
    assert run_cli.main(["-", "--model", "anthropic/m", "--cwd", str(tmp_path)]) == 0
    assert "piped" in capsys.readouterr().out


def test_the_module_runs_as_a_program(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "abp_run", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--approve" in proc.stdout


# ---- abp_acp ------------------------------------------------------------------------------------------------
class Client:
    """A stand-in editor: feeds lines to the server and records what it sends back."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.out: list[dict] = []
        self.cond = asyncio.Condition()

    async def read_line(self):
        return await self.inbox.get()

    async def write_line(self, text):
        async with self.cond:
            self.out.append(json.loads(text))
            self.cond.notify_all()

    def send(self, **message):
        self.inbox.put_nowait(json.dumps({"jsonrpc": "2.0", **message}))

    async def wait_for(self, predicate, timeout=20):
        async def loop():
            async with self.cond:
                await self.cond.wait_for(lambda: any(predicate(m) for m in self.out))
            return next(m for m in self.out if predicate(m))

        return await asyncio.wait_for(loop(), timeout)

    def answer(self, request, result):
        self.send(id=request["id"], result=result)


def make_runner(transport, tmp_path):
    async def runner(prompt, session, on_text, progress, approval_notify):
        return await core.run_turn(prompt, transport=transport, model="m", cwd=session.cwd, on_text=on_text, timeout_s=60,
                                   extra_context={"desktop_session_key": f"acp:{session.id}", "progress_notify": progress,
                                                  "approval_notify": approval_notify, "chat_id": session.id, "instance_id": 0})

    return runner


def acp(tmp_path, transport, body, approve="ask"):
    async def main():
        client = Client()
        server = AcpServer(client.read_line, client.write_line, make_runner(transport, tmp_path))
        serving = asyncio.create_task(server.serve())
        try:
            await body(client, server)
        finally:
            client.inbox.put_nowait(None)
            await asyncio.wait_for(serving, 30)

    root = tmp_path / "state"
    root.mkdir()
    with core.ephemeral_environment(root, approve):
        asyncio.run(main())


async def start_session(client, cwd, base=1):
    client.send(id=base, method="initialize", params={"protocolVersion": 1, "clientCapabilities": {}})
    init = await client.wait_for(lambda m: m.get("id") == base)
    client.send(id=base + 1, method="session/new", params={"cwd": str(cwd), "mcpServers": []})
    new = await client.wait_for(lambda m: m.get("id") == base + 1)
    return init, new["result"]["sessionId"]


def test_initialize_and_a_prompt_stream_back_a_reply(tmp_path):
    (tmp_path / "config.json").write_text('{"port": 8123}')

    async def body(client, server):
        init, sid = await start_session(client, tmp_path)
        assert init["result"]["protocolVersion"] == 1 and init["result"]["agentInfo"]["name"] == "abp"
        assert init["result"]["agentCapabilities"]["promptCapabilities"]["embeddedContext"] is True
        client.send(id=3, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": "what port?"}]})
        done = await client.wait_for(lambda m: m.get("id") == 3)
        assert done["result"] == {"stopReason": "end_turn"}
        updates = [m["params"]["update"] for m in client.out if m.get("method") == "session/update"]
        text = "".join(u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk")
        assert text == "8123"
        assert any(u["sessionUpdate"] == "tool_call" and u["status"] == "in_progress" for u in updates)
        assert any(u["sessionUpdate"] == "tool_call_update" and u["status"] == "completed" for u in updates)

    acp(tmp_path, script(Call("read_file", {"path": "config.json"}), Say("8123")), body)


def test_a_second_prompt_in_the_same_session_remembers_the_first(tmp_path):
    seen = []

    class Remember(ScriptedTransport):
        async def send(self, **kw):
            seen.append(json.dumps(kw["history"]))
            return await super().send(**kw)

    async def body(client, server):
        _, sid = await start_session(client, tmp_path)
        for n, word in ((3, "alpha"), (4, "beta")):
            client.send(id=n, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": f"remember {word}"}]})
            await client.wait_for(lambda m, n=n: m.get("id") == n)
        assert "remember alpha" in seen[-1] and "remember beta" in seen[-1]

    acp(tmp_path, Remember([Say("ok"), Say("ok2")]), body)


def test_permission_is_asked_of_the_editor_and_its_answer_is_obeyed(tmp_path):
    async def body(client, server):
        _, sid = await start_session(client, tmp_path)
        client.send(id=3, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": "write it"}]})
        ask = await client.wait_for(lambda m: m.get("method") == "session/request_permission")
        assert ask["params"]["sessionId"] == sid and "write_file" in ask["params"]["toolCall"]["title"]
        assert {o["kind"] for o in ask["params"]["options"]} == {"allow_once", "allow_always", "reject_once"}
        client.answer(ask, {"outcome": {"outcome": "selected", "optionId": "allow_once"}})
        assert (await client.wait_for(lambda m: m.get("id") == 3))["result"]["stopReason"] == "end_turn"
        assert (tmp_path / "x.txt").read_text() == "hi"

    acp(tmp_path, script(Call("write_file", {"path": "x.txt", "content": "hi"}), Say("done")), body)


def test_a_rejected_or_cancelled_permission_stops_the_action(tmp_path):
    async def body(client, server):
        _, sid = await start_session(client, tmp_path)
        client.send(id=3, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": "write it"}]})
        ask = await client.wait_for(lambda m: m.get("method") == "session/request_permission")
        client.answer(ask, {"outcome": {"outcome": "cancelled"}})
        await client.wait_for(lambda m: m.get("id") == 3)
        assert not (tmp_path / "x.txt").exists()

    acp(tmp_path, script(Call("write_file", {"path": "x.txt", "content": "hi"}), Say("done")), body)


def test_cancel_ends_the_turn_with_a_cancelled_stop_reason(tmp_path):
    class Slow(ScriptedTransport):
        async def send(self, **kw):
            await asyncio.sleep(30)

    async def body(client, server):
        _, sid = await start_session(client, tmp_path)
        client.send(id=3, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.5)
        client.send(method="session/cancel", params={"sessionId": sid})
        assert (await client.wait_for(lambda m: m.get("id") == 3))["result"] == {"stopReason": "cancelled"}

    acp(tmp_path, Slow([]), body)


def test_bad_requests_get_proper_errors(tmp_path):
    async def body(client, server):
        client.send(id=1, method="no/such", params={})
        assert (await client.wait_for(lambda m: m.get("id") == 1))["error"]["code"] == -32601
        client.send(id=2, method="session/new", params={"cwd": "relative/path"})
        assert (await client.wait_for(lambda m: m.get("id") == 2))["error"]["code"] == -32602
        client.send(id=3, method="session/prompt", params={"sessionId": "nope", "prompt": [{"type": "text", "text": "x"}]})
        assert (await client.wait_for(lambda m: m.get("id") == 3))["error"]["code"] == -32602
        _, sid = await start_session(client, tmp_path, base=10)
        client.send(id=5, method="session/prompt", params={"sessionId": sid, "prompt": []})
        assert (await client.wait_for(lambda m: m.get("id") == 5))["error"]["code"] == -32602
        client.inbox.put_nowait("this is not json")
        assert (await client.wait_for(lambda m: m.get("error", {}).get("code") == -32700))

    acp(tmp_path, script(), body)


def test_a_failed_run_becomes_an_error_response(tmp_path):
    from bot.backends.base import BackendError

    class Boom(ScriptedTransport):
        async def send(self, **kw):
            raise BackendError("the model is down")

    async def body(client, server):
        _, sid = await start_session(client, tmp_path)
        client.send(id=3, method="session/prompt", params={"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]})
        err = (await client.wait_for(lambda m: m.get("id") == 3))["error"]
        assert err["code"] == -32603 and "the model is down" in err["message"]

    acp(tmp_path, Boom([]), body)


def test_prompt_blocks_become_text_with_their_context():
    text = prompt_text([{"type": "text", "text": "fix this"},
                        {"type": "resource", "resource": {"uri": "file:///a.py", "text": "print(1)"}},
                        {"type": "resource_link", "uri": "file:///b.py", "name": "b.py"},
                        {"type": "image", "data": "..", "mimeType": "image/png"}])
    assert text.startswith("fix this") and "[file:///a.py]\nprint(1)" in text and "file:///b.py" in text and "image was attached" in text
    assert prompt_text([]) == ""


def test_the_server_works_over_real_pipes(tmp_path):
    """Start the actual program and speak to it through its standard input and output."""
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("ABP_AGENT", "ABP_CICD"))}
    env.update(ABP_RUN_MODEL="anthropic/unused", ANTHROPIC_API_KEY="unused")
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n"
    proc = subprocess.Popen([sys.executable, "-m", "abp_acp", "--model", "anthropic/unused"], cwd=ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(request, timeout=120)        # stdin is closed after the request, so the server ends by itself
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"the server did not finish\nstdout: {out!r}\nstderr: {err[-2000:]}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"no reply\nstderr: {err[-2000:]}"
    reply = json.loads(lines[0])
    assert reply["id"] == 1 and reply["result"]["agentInfo"]["name"] == "abp", (out, err[-2000:])
    assert proc.returncode == 0, err[-2000:]
