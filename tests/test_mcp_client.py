"""bot/agent_runtime/mcp_client.py — the external-MCP-server tool
bridge. `mcp` itself is only imported lazily inside _open_session() (see
that module's docstring), so these tests never need the real `mcp`
package installed in this dev shell (it lives only in the pipeline's
bundled venv, same as tests/test_mcp_server_tools.py already notes) —
connect()'s own real-network path is exercised by monkeypatching
_open_session, never by talking to a real subprocess/URL.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import db
from bot.agent_runtime import mcp_client


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_registry():
    mcp_client._connections.clear()
    mcp_client._tool_index.clear()
    yield
    mcp_client._connections.clear()
    mcp_client._tool_index.clear()


def _fake_connection(name, tools):
    from contextlib import AsyncExitStack

    return mcp_client._Connection(name=name, stack=AsyncExitStack(), session=object(), tools=tools)


def test_external_tool_schemas_are_namespaced():
    mcp_client._connections["github"] = _fake_connection(
        "github", [{"name": "search_repos", "description": "search", "input_schema": {"type": "object"}}]
    )
    mcp_client._rebuild_tool_index()

    schemas = mcp_client.external_tool_schemas()

    assert schemas == [{"name": "mcp_github_search_repos", "description": "[github] search", "input_schema": {"type": "object"}}]
    assert mcp_client.has_tool("mcp_github_search_repos")
    assert not mcp_client.has_tool("search_repos")


def test_no_connections_means_no_schemas():
    assert mcp_client.external_tool_schemas() == []
    assert mcp_client.connected_servers() == []


def test_connect_returns_false_when_no_row_exists(temp_db):
    assert _run(mcp_client.connect("nope")) is False
    assert mcp_client.connected_servers() == []


def test_connect_returns_false_when_row_is_disabled(temp_db):
    db.add_external_mcp_server("github", "stdio", command="npx", args_json='["-y","@modelcontextprotocol/server-github"]')
    db.set_external_mcp_server_enabled("github", False)

    assert _run(mcp_client.connect("github")) is False


def test_connect_registers_the_connection_on_success(temp_db, monkeypatch):
    db.add_external_mcp_server("github", "stdio", command="npx")

    async def _fake_open_session(name, **kwargs):
        return _fake_connection(name, [{"name": "search_repos", "description": "search", "input_schema": {}}])

    monkeypatch.setattr(mcp_client, "_open_session", _fake_open_session)

    ok = _run(mcp_client.connect("github"))

    assert ok is True
    assert mcp_client.connected_servers() == ["github"]
    assert mcp_client.has_tool("mcp_github_search_repos")


def test_connect_failure_is_logged_and_swallowed_not_raised(temp_db, monkeypatch):
    db.add_external_mcp_server("broken", "stdio", command="does-not-exist")

    async def _fake_open_session(name, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(mcp_client, "_open_session", _fake_open_session)

    ok = _run(mcp_client.connect("broken"))

    assert ok is False
    assert mcp_client.connected_servers() == []


def test_connect_all_enabled_skips_disabled_rows(temp_db, monkeypatch):
    db.add_external_mcp_server("on", "stdio", command="npx")
    db.add_external_mcp_server("off", "stdio", command="npx")
    db.set_external_mcp_server_enabled("off", False)

    calls = []

    async def _fake_connect(name):
        calls.append(name)
        return True

    monkeypatch.setattr(mcp_client, "connect", _fake_connect)

    _run(mcp_client.connect_all_enabled())

    assert calls == ["on"]


def test_disconnect_closes_the_stack_and_clears_the_index():
    closed = []

    class _FakeStack:
        async def aclose(self):
            closed.append(True)

    mcp_client._connections["github"] = mcp_client._Connection(name="github", stack=_FakeStack(), session=object(), tools=[])
    mcp_client._rebuild_tool_index()

    _run(mcp_client.disconnect("github"))

    assert closed == [True]
    assert mcp_client.connected_servers() == []


class _FakeToolBlock:
    def __init__(self, text):
        self.text = text


class _FakeCallToolResult:
    def __init__(self, text, is_error=False):
        self.content = [_FakeToolBlock(text)]
        self.is_error = is_error


class _FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


def test_call_tool_routes_to_the_right_server_and_real_tool_name():
    session = _FakeSession(_FakeCallToolResult("42 repos found"))
    mcp_client._connections["github"] = mcp_client._Connection(name="github", stack=object(), session=session, tools=[])
    mcp_client._tool_index["mcp_github_search_repos"] = ("github", "search_repos")

    out = _run(mcp_client.call_tool("mcp_github_search_repos", {"q": "botserver"}))

    assert out == "42 repos found"
    assert session.calls == [("search_repos", {"q": "botserver"})]


def test_call_tool_marks_a_tool_error():
    session = _FakeSession(_FakeCallToolResult("bad request", is_error=True))
    mcp_client._connections["github"] = mcp_client._Connection(name="github", stack=object(), session=session, tools=[])
    mcp_client._tool_index["mcp_github_search_repos"] = ("github", "search_repos")

    out = _run(mcp_client.call_tool("mcp_github_search_repos", {}))

    assert out == "[tool error] bad request"


def test_call_tool_raises_if_the_server_disconnected_between_schema_build_and_call():
    mcp_client._tool_index["mcp_github_search_repos"] = ("github", "search_repos")

    with pytest.raises(RuntimeError, match="no longer connected"):
        _run(mcp_client.call_tool("mcp_github_search_repos", {}))


# ------------------------------------------------------- db CRUD --------

def test_db_add_list_get_enable_delete_round_trip(temp_db):
    db.add_external_mcp_server("github", "stdio", command="npx", args_json='["-y"]')
    row = db.get_external_mcp_server("github")
    assert row["transport"] == "stdio"
    assert bool(row["enabled"]) is True

    db.set_external_mcp_server_enabled("github", False)
    assert bool(db.get_external_mcp_server("github")["enabled"]) is False

    assert len(db.list_external_mcp_servers()) == 1
    assert db.delete_external_mcp_server("github") is True
    assert db.get_external_mcp_server("github") is None
    assert db.delete_external_mcp_server("github") is False


def test_db_list_scoped_to_an_instance_includes_global_rows(temp_db):
    from bot import bot_instances

    iid = bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )
    db.add_external_mcp_server("global-one", "stdio", command="npx")
    db.add_external_mcp_server("scoped-one", "stdio", command="npx", instance_id=iid)
    other_iid = bot_instances.create_instance(
        name="worker-2", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )
    db.add_external_mcp_server("other-scoped", "stdio", command="npx", instance_id=other_iid)

    names = {row["name"] for row in db.list_external_mcp_servers(iid)}
    assert names == {"global-one", "scoped-one"}
    assert {row["name"] for row in db.list_external_mcp_servers()} == {"global-one", "scoped-one", "other-scoped"}
