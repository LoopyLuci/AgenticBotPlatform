"""bot/plugins.py — the local plugin registry that lets a Python file
already on disk register new agent tools and slash commands. Exercises
real plugin.py files loaded from tmp_path (matching bot/skills.py's own
"local install, not mocked" test standard), against a real temp_db so
install/enable/disable state actually persists.
"""

from __future__ import annotations

import asyncio

import pytest

from bot import commands as bot_commands
from bot import plugins


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_registry():
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()
    yield
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()


TOOL_PLUGIN = '''
PLUGIN_DESCRIPTION = "Adds a greeting tool"

async def _greet(tool_input, *, workspace, instance_id):
    return f"hello, {tool_input.get('who', 'world')}!"

def setup(api):
    api.register_tool(
        "greet",
        "Say hello",
        {"type": "object", "properties": {"who": {"type": "string"}}},
        _greet,
    )
'''

COMMAND_PLUGIN = '''
PLUGIN_DESCRIPTION = "Adds a /ping command"

async def _ping(ctx, args):
    return "pong"

def setup(api):
    api.register_command("ping", "Replies pong", _ping, aliases=("pingme",))
'''

BROKEN_PLUGIN = '''
def setup(api):
    raise RuntimeError("boom")
'''


def _write(tmp_path, name: str, content: str):
    p = tmp_path / f"{name}.py"
    p.write_text(content, encoding="utf-8")
    return p


def test_install_tool_plugin_registers_tool(tmp_path, temp_db):
    p = _write(tmp_path, "greeter", TOOL_PLUGIN)
    info = plugins.install(str(p))
    assert info["name"] == "greeter"
    assert info["tools"] == ["greet"]
    assert plugins.has_tool("greet")
    schemas = plugins.tool_schemas()
    assert schemas[0]["name"] == "greet"


def test_execute_plugin_tool(tmp_path, temp_db):
    p = _write(tmp_path, "greeter", TOOL_PLUGIN)
    plugins.install(str(p))
    result = _run(plugins.execute_tool("greet", {"who": "claude"}, workspace=tmp_path, instance_id=None))
    assert result == "hello, claude!"


def test_install_command_plugin_and_dispatch(tmp_path, temp_db):
    p = _write(tmp_path, "pinger", COMMAND_PLUGIN)
    info = plugins.install(str(p))
    assert info["commands"] == ["ping"]
    assert plugins.resolve_command("ping") == "ping"
    assert plugins.resolve_command("pingme") == "ping"


def test_dispatch_command_reaches_plugin_command(tmp_path, temp_db):
    p = _write(tmp_path, "pinger", COMMAND_PLUGIN)
    plugins.install(str(p))
    ctx = bot_commands.CmdContext(instance_id=1, instance_name="x", user_id=1, chat_id=1, actor="test")
    reply = _run(bot_commands.dispatch_command("/ping", ctx))
    assert reply == "pong"


def test_setup_failure_leaves_nothing_registered(tmp_path, temp_db):
    p = _write(tmp_path, "broken", BROKEN_PLUGIN)
    with pytest.raises(plugins.PluginError):
        plugins.install(str(p))
    assert plugins.list_plugins() == []


def test_register_tool_rejects_builtin_name_collision(tmp_path, temp_db):
    content = '''
def setup(api):
    async def h(tool_input, *, workspace, instance_id):
        return ""
    api.register_tool("read_file", "shadow", {"type": "object", "properties": {}}, h)
'''
    p = _write(tmp_path, "shadower", content)
    with pytest.raises(plugins.PluginError):
        plugins.install(str(p))


def test_register_command_rejects_builtin_name_collision(tmp_path, temp_db):
    content = '''
def setup(api):
    async def h(ctx, args):
        return ""
    api.register_command("status", "shadow", h)
'''
    p = _write(tmp_path, "shadower2", content)
    with pytest.raises(plugins.PluginError):
        plugins.install(str(p))


def test_disable_unregisters_but_keeps_row(tmp_path, temp_db):
    p = _write(tmp_path, "greeter", TOOL_PLUGIN)
    plugins.install(str(p))
    info = plugins.disable("greeter")
    assert info["enabled"] is False
    assert not plugins.has_tool("greet")
    rows = plugins.list_plugins()
    assert rows[0]["name"] == "greeter"
    assert rows[0]["enabled"] is False


def test_enable_reloads_from_disk(tmp_path, temp_db):
    p = _write(tmp_path, "greeter", TOOL_PLUGIN)
    plugins.install(str(p))
    plugins.disable("greeter")
    info = plugins.enable("greeter")
    assert info["enabled"] is True
    assert plugins.has_tool("greet")


def test_remove_deletes_row_and_unregisters(tmp_path, temp_db):
    p = _write(tmp_path, "greeter", TOOL_PLUGIN)
    plugins.install(str(p))
    assert plugins.remove("greeter") is True
    assert plugins.list_plugins() == []
    assert not plugins.has_tool("greet")


def test_load_enabled_skips_broken_plugin_without_raising(tmp_path, temp_db):
    good = _write(tmp_path, "greeter", TOOL_PLUGIN)
    plugins.install(str(good))
    plugins.disable("greeter")
    # Simulate a broken plugin row that was somehow left enabled — must not
    # crash the whole startup path.
    from bot import db

    db.install_plugin_row("nonexistent", str(tmp_path / "does_not_exist.py"), "")
    plugins.load_enabled()  # should log and continue, not raise
    assert not plugins.has_tool("greet")  # greeter was disabled, stays unloaded
