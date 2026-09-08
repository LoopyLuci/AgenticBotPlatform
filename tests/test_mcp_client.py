"""bot/agent_runtime/mcp_client.py — the external-MCP-server tool
bridge, plus its sampling and OAuth support. `mcp` itself is only
imported lazily inside _build_session()/handle_sampling_request() (see
that module's docstring), so most of these tests never need the real
`mcp` package installed in this dev shell (it lives only in the
pipeline's bundled venv, same as tests/test_mcp_server_tools.py already
notes) — connect()'s own real-network path is exercised by monkeypatching
_build_session, never by talking to a real subprocess/URL. The real
package IS exercised live outside pytest (see the module's own docstring
and this session's manual smoke tests) against a genuine stdio server
subprocess for both tool-calling and sampling.
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
    mcp_client._owner_tasks.clear()
    mcp_client._pending_oauth.clear()
    mcp_client._oauth_state_to_server.clear()
    yield
    mcp_client._connections.clear()
    mcp_client._tool_index.clear()
    mcp_client._owner_tasks.clear()
    mcp_client._pending_oauth.clear()
    mcp_client._oauth_state_to_server.clear()


def _fake_connection(name, tools):
    return mcp_client._Connection(name=name, session=object(), tools=tools)


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

    async def _fake_build_session(stack, name, **kwargs):
        return object(), [{"name": "search_repos", "description": "search", "input_schema": {}}]

    monkeypatch.setattr(mcp_client, "_build_session", _fake_build_session)

    ok = _run(mcp_client.connect("github"))

    assert ok is True
    assert mcp_client.connected_servers() == ["github"]
    assert mcp_client.has_tool("mcp_github_search_repos")


def test_connect_failure_is_logged_and_swallowed_not_raised(temp_db, monkeypatch):
    db.add_external_mcp_server("broken", "stdio", command="does-not-exist")

    async def _fake_build_session(stack, name, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(mcp_client, "_build_session", _fake_build_session)

    ok = _run(mcp_client.connect("broken"))

    assert ok is False
    assert mcp_client.connected_servers() == []


def test_connect_returns_false_and_keeps_running_when_it_exceeds_wait_s(temp_db, monkeypatch):
    """The background-task design (see connect()'s own docstring): a slow
    connection attempt (modeling an OAuth authorization wait) doesn't
    block the caller past `wait_s` — but it isn't cancelled either, and
    still registers normally once it actually finishes."""
    db.add_external_mcp_server("slow", "stdio", command="npx")
    release = asyncio.Event()

    async def _fake_build_session(stack, name, **kwargs):
        await release.wait()
        return object(), [{"name": "t", "description": "", "input_schema": {}}]

    monkeypatch.setattr(mcp_client, "_build_session", _fake_build_session)

    async def scenario():
        ok = await mcp_client.connect("slow", wait_s=0.05)
        assert ok is False
        assert mcp_client.connected_servers() == []

        release.set()
        await asyncio.sleep(0.05)
        assert mcp_client.connected_servers() == ["slow"]

    _run(scenario())


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


def test_disconnect_signals_close_event_and_awaits_the_owner_task():
    async def scenario():
        closed = []
        ready = asyncio.Event()

        async def _owner():
            conn = mcp_client._Connection(name="github", session=object(), tools=[])
            mcp_client._connections["github"] = conn
            mcp_client._rebuild_tool_index()
            ready.set()
            await conn.close_event.wait()
            closed.append(True)

        task = asyncio.create_task(_owner())
        mcp_client._owner_tasks["github"] = task
        await ready.wait()

        await mcp_client.disconnect("github")

        assert closed == [True]
        assert mcp_client.connected_servers() == []

    _run(scenario())


def test_disconnect_cancels_an_owner_task_still_stuck_before_registering():
    """A pending OAuth flow (or a slow subprocess spawn) has an owner task
    running but no _Connection registered yet — disconnect() must still
    tear it down, via cancellation since there's no close_event to signal."""

    async def scenario():
        started = asyncio.Event()

        async def _owner():
            started.set()
            await asyncio.Event().wait()  # never set — only cancellation ends this

        task = asyncio.create_task(_owner())
        mcp_client._owner_tasks["stuck"] = task
        await started.wait()

        await mcp_client.disconnect("stuck")

        assert task.cancelled()
        assert "stuck" not in mcp_client._owner_tasks

    _run(scenario())


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
    mcp_client._connections["github"] = mcp_client._Connection(name="github", session=session, tools=[])
    mcp_client._tool_index["mcp_github_search_repos"] = ("github", "search_repos")

    out = _run(mcp_client.call_tool("mcp_github_search_repos", {"q": "botserver"}))

    assert out == "42 repos found"
    assert session.calls == [("search_repos", {"q": "botserver"})]


def test_call_tool_marks_a_tool_error():
    session = _FakeSession(_FakeCallToolResult("bad request", is_error=True))
    mcp_client._connections["github"] = mcp_client._Connection(name="github", session=session, tools=[])
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
    assert bool(row["oauth_enabled"]) is False

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


def test_db_oauth_client_info_and_tokens_round_trip(temp_db):
    db.add_external_mcp_server("remote-one", "remote", url="https://example.com/mcp", oauth_enabled=True)

    db.set_external_mcp_oauth_client_info("remote-one", '{"client_id": "abc"}')
    db.set_external_mcp_oauth_tokens("remote-one", '{"access_token": "xyz"}')

    row = db.get_external_mcp_server("remote-one")
    assert row["oauth_client_info_json"] == '{"client_id": "abc"}'
    assert row["oauth_tokens_json"] == '{"access_token": "xyz"}'
