"""/mcp_external (bot.commands.cmd_mcp_external) — distinct from the
existing /mcp command, which manages Claude Desktop's own MCP server
list for the `ui` backend. mcp_client.connect()/disconnect() are
monkeypatched so this never touches the real `mcp` package (only in the
pipeline's bundled venv — see tests/test_mcp_client.py's own docstring).
"""
from __future__ import annotations

import asyncio

import pytest

from bot import commands, db
from bot.agent_runtime import mcp_client
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return CmdContext(instance_id=1, instance_name="test", user_id=1, chat_id=1, actor="test")


@pytest.fixture(autouse=True)
def _fake_connect(monkeypatch):
    async def _connect(name):
        return True

    async def _disconnect(name):
        return None

    monkeypatch.setattr(mcp_client, "connect", _connect)
    monkeypatch.setattr(mcp_client, "disconnect", _disconnect)
    monkeypatch.setattr(mcp_client, "connected_servers", lambda: [])


def test_list_when_empty(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), []))
    assert "No external MCP servers" in reply


def test_add_stdio_then_list(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), ["add", "github", "stdio", "npx", "-y", "server-github"]))
    assert "Added" in reply
    assert "Connected" in reply

    row = db.get_external_mcp_server("github")
    assert row["transport"] == "stdio"
    assert row["command"] == "npx"

    listed = _run(commands.cmd_mcp_external(_ctx(), ["list"]))
    assert "github" in listed


def test_add_remote(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), ["add", "docs", "remote", "https://example.com/mcp", "secret"]))
    assert "Added" in reply
    row = db.get_external_mcp_server("docs")
    assert row["transport"] == "remote"
    assert row["url"] == "https://example.com/mcp"
    assert row["auth_token"] == "secret"


def test_add_rejects_duplicate_name(temp_db):
    _run(commands.cmd_mcp_external(_ctx(), ["add", "github", "stdio", "npx"]))
    reply = _run(commands.cmd_mcp_external(_ctx(), ["add", "github", "stdio", "npx"]))
    assert "already exists" in reply


def test_add_rejects_unknown_transport(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), ["add", "x", "carrier-pigeon", "whatever"]))
    assert "stdio" in reply and "remote" in reply


def test_enable_disable_remove_round_trip(temp_db):
    _run(commands.cmd_mcp_external(_ctx(), ["add", "github", "stdio", "npx"]))

    reply = _run(commands.cmd_mcp_external(_ctx(), ["disable", "github"]))
    assert "Disabled" in reply
    assert bool(db.get_external_mcp_server("github")["enabled"]) is False

    reply = _run(commands.cmd_mcp_external(_ctx(), ["enable", "github"]))
    assert "Enabled" in reply
    assert bool(db.get_external_mcp_server("github")["enabled"]) is True

    reply = _run(commands.cmd_mcp_external(_ctx(), ["remove", "github"]))
    assert "Removed" in reply
    assert db.get_external_mcp_server("github") is None


def test_enable_unknown_name(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), ["enable", "nope"]))
    assert "No external MCP server named" in reply


def test_usage_on_bad_args(temp_db):
    reply = _run(commands.cmd_mcp_external(_ctx(), ["nonsense"]))
    assert "Usage:" in reply
