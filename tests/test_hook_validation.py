"""validate_hook_command (bot/agent_runtime/hooks.py) and every path that adds a hook: the dashboard route, the
/hooks add command and the MCP add_hook tool."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from bot import commands, db
from bot.agent_runtime.hooks import validate_hook_command
from bot.dashboard.server import build_app

GOOD = [
    "echo hi",
    "https://example.com/hook",                      # plain text: today's behaviour, never inspected
    '{"type": "command", "command": "echo hi", "async": true, "timeout": 5, "fail": "closed"}',
    '{"type": "http", "url": "https://example.com/h", "async": true}',
    '{"type": "mcp_tool", "server": "s", "tool": "t", "fail": "open"}',
    '{"type": "prompt", "prompt": "is this safe?", "timeout": 30.5}',
    '{"type": "agent", "agent": "reviewer", "prompt": "check it"}',
]
BAD = [
    ('{"type": "command"', "valid JSON"),
    ('{"command": "x"}', "'type'"),
    ('{"type": "ftp", "url": "x"}', "'type'"),
    ('{"type": "command"}', "'command'"),
    ('{"type": "command", "command": "  "}', "'command'"),
    ('{"type": "http", "url": "ftp://example.com/x"}', "http or https"),
    ('{"type": "http", "url": "example.com"}', "http or https"),
    ('{"type": "mcp_tool", "server": "s"}', "'tool'"),
    ('{"type": "prompt"}', "'prompt'"),
    ('{"type": "agent", "agent": "a"}', "'prompt'"),
    ('{"type": "prompt", "prompt": "p", "async": true}', "async"),
    ('{"type": "agent", "agent": "a", "prompt": "p", "async": true}', "async"),
    ('{"type": "command", "command": "x", "async": "yes"}', "async"),
    ('{"type": "command", "command": "x", "timeout": 0}', "timeout"),
    ('{"type": "command", "command": "x", "timeout": -3}', "timeout"),
    ('{"type": "command", "command": "x", "timeout": 100000}', "timeout"),
    ('{"type": "command", "command": "x", "timeout": "5"}', "timeout"),
    ('{"type": "command", "command": "x", "timeout": true}', "timeout"),
    ('{"type": "command", "command": "x", "fail": "maybe"}', "fail"),
]


@pytest.mark.parametrize("text", GOOD)
def test_good_hooks_pass(text):
    assert validate_hook_command(text) is None


@pytest.mark.parametrize("text,word", BAD)
def test_bad_typed_hooks_say_why(text, word):
    problem = validate_hook_command(text)
    assert problem and word in problem


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


H = {"X-Dashboard-Token": "test-token"}


def test_route_rejects_a_bad_typed_hook_with_400_and_stores_nothing(temp_db, monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/api/hooks", json={"event": "PreToolUse", "command": '{"type": "http", "url": "ftp://x"}'}, headers=H)
    assert r.status_code == 400 and "http or https" in r.json()["detail"]
    assert client.get("/api/hooks", headers=H).json()["hooks"] == []
    ok = client.post("/api/hooks", json={"event": "PreToolUse", "command": json.dumps({"type": "prompt", "prompt": "p"})}, headers=H)
    assert ok.status_code == 200 and len(client.get("/api/hooks", headers=H).json()["hooks"]) == 1


def test_hooks_add_command_rejects_and_accepts(temp_db):
    run = lambda *a: asyncio.run(commands.cmd_hooks(None, list(a)))      # noqa: E731
    out = run("add", "PreToolUse", "*", '{"type":', '"nope"}')
    assert out.startswith("Hook rejected") and db.list_agent_hooks() == []
    assert run("add", "PreToolUse", "*", "echo", "hi").startswith("Added hook")
    assert run("add", "PreToolUse", "*", '{"type":', '"command",', '"command":', '"echo"}').startswith("Added hook")


def test_mcp_add_hook_refuses_before_calling_the_dashboard(monkeypatch):
    from bot import mcp_server

    async def boom(*a, **k):
        raise AssertionError("must not reach the dashboard")

    monkeypatch.setattr(mcp_server, "_request", boom)
    fn = getattr(mcp_server.add_hook, "fn", mcp_server.add_hook)
    out = asyncio.run(fn("PreToolUse", '{"type": "agent"}'))
    assert out["ok"] is False and "agent" in out["error"]
