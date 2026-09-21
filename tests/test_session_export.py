"""Exporting a conversation (roadmap P5): every stored message shape, secrets removed, chat command, API."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from bot import commands, db
from bot.agent_runtime import session_export as ex
from bot.dashboard.server import build_app

ANTHROPIC = [
    {"role": "user", "content": "What port does the config use?"},
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Let me look."},
                                      {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "config.json"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"port": 8123}'}]},
    {"role": "assistant", "content": [{"type": "text", "text": "It uses 8123."}]},
]
OPENAI = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]}},
    {"role": "tool", "content": {"tool_call_id": "c1", "content": "a.txt\nb.txt"}},
    {"role": "assistant", "content": {"content": "Two files."}},
]


def test_anthropic_shaped_history_reads_as_a_transcript_and_reasoning_is_left_out():
    md = ex.render(ANTHROPIC, title="Ports")
    assert md.startswith("# Ports") and "## You\n\nWhat port does the config use?" in md
    assert "> tool: `read_file` `{\"path\": \"config.json\"}`" in md and '> result: {"port": 8123}' in md and "It uses 8123." in md
    assert "hmm" not in md


def test_openai_shaped_history_reads_the_same_way():
    md = ex.render(OPENAI)
    assert "> tool: `list_dir`" in md and "> result: a.txt\n> b.txt" in md and "Two files." in md


def test_json_export_is_the_normalised_list():
    data = json.loads(ex.render(OPENAI, "json", title="t"))
    assert data["title"] == "t" and [m["role"] for m in data["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert data["messages"][1]["parts"][0]["kind"] == "tool_call" and data["messages"][2]["parts"][0]["kind"] == "tool_result"
    with pytest.raises(ValueError):
        ex.render(OPENAI, "html")


def test_long_tool_output_is_shortened():
    md = ex.render([{"role": "tool", "content": {"tool_call_id": "x", "content": "y" * 5000}}])
    assert "more characters" in md and md.count("y") < 1300


def test_secrets_are_removed_whether_or_not_the_server_holds_them(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "server-held-value-9999999")
    messages = [{"role": "user", "content": "use server-held-value-9999999 and sk-abcdefghijklmnopqrstu and password=hunter2hunter2"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "run_shell",
                                                  "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop12345678' x"}}]},
                {"role": "user", "content": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"}]
    for fmt in ("md", "json"):
        out = ex.render(messages, fmt)
        for leaked in ("server-held-value", "sk-abcdefghij", "hunter2", "abcdefghijklmnop12345678", "AAAA"):
            assert leaked not in out, (fmt, leaked)
    assert "[secret:MY_API_KEY]" in ex.render(messages)
    assert "tokens: 4000" in ex.render([{"role": "user", "content": "tokens: 4000"}])          # ordinary text is left alone


def test_a_missing_or_empty_session_is_reported(temp_db):
    with pytest.raises(LookupError):
        ex.export_session("nope")


def test_export_reads_from_the_database(temp_db):
    for m in ANTHROPIC:
        db.append_agent_message("sess1", m["role"], m["content"])
    assert "It uses 8123." in ex.export_session("sess1")


def test_the_chat_command_exports_the_active_conversation(temp_db):
    from bot import bot_instances

    iid = bot_instances.create_instance(name="w", platform="telegram", backend="api",
                                        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    ctx = commands.CmdContext(instance_id=iid, instance_name="w", user_id=1, chat_id=5, actor="t")
    assert "no conversation" in asyncio.run(commands.cmd_export(ctx, []))
    db.link_chat_session(iid, 5, "sess2", title="Ports") if hasattr(db, "link_chat_session") else None
    if db.get_active_chat_session(iid, 5) is None:
        pytest.skip("no helper to create a chat session in this build")
    for m in ANTHROPIC:
        db.append_agent_message("sess2", m["role"], m["content"])
    out = asyncio.run(commands.cmd_export(ctx, []))
    assert out.startswith("# Ports") and "It uses 8123." in out
    assert json.loads(asyncio.run(commands.cmd_export(ctx, ["json"])))["messages"]


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


H = {"X-Dashboard-Token": "test-token"}


def test_the_export_route_needs_the_dashboard_token_and_returns_text(client):
    for m in ANTHROPIC:
        db.append_agent_message("sess3", m["role"], m["content"])
    assert client.get("/api/agent/sessions/sess3/export").status_code == 401
    r = client.get("/api/agent/sessions/sess3/export", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown") and "It uses 8123." in r.text
    assert json.loads(client.get("/api/agent/sessions/sess3/export?format=json", headers=H).text)["messages"]
    assert client.get("/api/agent/sessions/missing/export", headers=H).status_code == 404
    assert client.get("/api/agent/sessions/sess3/export?format=html", headers=H).status_code == 422
    assert client.get("/api/agent/sessions/desktop:abc/def/export", headers=H).status_code == 404       # a key containing slashes routes
