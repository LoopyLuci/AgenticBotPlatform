"""create_plugin/enable_plugin/disable_plugin/remove_plugin/list_plugins —
the agent-facing tools that let an agent author a brand-new callable tool
by writing real Python code, per the user's explicit choice that this
requires human approval before it can run (the same trust boundary
run_shell/write_file already sit behind — see
docs/adr/0007-plugins-are-trusted-local-code.md).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import bot_instances, plugins
from bot.agent_runtime import tools as agent_tools


def _run(coro):
    return asyncio.run(coro)


def _create_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


def _exec(name, tool_input, *, instance_id, workspace):
    return _run(agent_tools.execute_tool(name, tool_input, workspace=workspace, instance_id=instance_id))


@pytest.fixture(autouse=True)
def _reset_registry(tmp_path, monkeypatch):
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()
    # create_plugin writes to PROJECT_ROOT/data/plugins/<name>/plugin.py —
    # redirect that to a throwaway directory so tests never touch the real
    # repo's data/plugins/.
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path)
    yield
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()


PLUGIN_CODE = '''
"""Adds one tool for a test."""

async def _handler(tool_input, *, workspace, instance_id):
    return "hello from the authored plugin"


def setup(api):
    api.register_tool("authored_tool", "A tool an agent wrote itself.", {"type": "object", "properties": {}}, _handler)
'''


def test_create_plugin_is_dangerous():
    assert agent_tools.is_dangerous("create_plugin")


def test_enable_plugin_is_dangerous():
    assert agent_tools.is_dangerous("enable_plugin")


def test_disable_and_remove_and_list_are_not_dangerous():
    assert not agent_tools.is_dangerous("disable_plugin")
    assert not agent_tools.is_dangerous("remove_plugin")
    assert not agent_tools.is_dangerous("list_plugins")


def test_create_plugin_writes_installs_and_immediately_activates(temp_db, tmp_path):
    """Confirmed against plugins.py's own install()->_activate() call
    chain: a freshly-installed plugin is enabled and loaded in the same
    call, no separate reload step needed — its tool must be usable in
    this very same turn."""
    instance_id = _create_instance()

    info = json.loads(_exec(
        "create_plugin",
        {"name": "authored", "description": "test plugin", "code": PLUGIN_CODE},
        instance_id=instance_id, workspace=tmp_path,
    ))
    assert info["name"] == "authored"
    assert info["enabled"] is True
    assert "authored_tool" in info["tools"]

    result = _run(agent_tools.execute_tool("authored_tool", {}, workspace=tmp_path, instance_id=instance_id))
    assert result == "hello from the authored plugin"

    # The file really landed on disk under the (redirected) data/plugins/,
    # named after the plugin (not a fixed "plugin.py" — install() derives
    # the registered name from the file's own stem).
    assert (tmp_path / "data" / "plugins" / "authored" / "authored.py").is_file()


def test_create_plugin_requires_name_and_code(temp_db, tmp_path):
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError, match="required"):
        _exec("create_plugin", {"name": "", "code": PLUGIN_CODE}, instance_id=instance_id, workspace=tmp_path)


def test_create_plugin_surfaces_a_bad_setup_as_a_tool_error(temp_db, tmp_path):
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError):
        _exec(
            "create_plugin", {"name": "broken", "code": "def setup(api):\n    raise RuntimeError('boom')\n"},
            instance_id=instance_id, workspace=tmp_path,
        )


def test_disable_then_enable_round_trip(temp_db, tmp_path):
    instance_id = _create_instance()
    _exec("create_plugin", {"name": "authored", "code": PLUGIN_CODE}, instance_id=instance_id, workspace=tmp_path)

    disabled = json.loads(_exec("disable_plugin", {"name": "authored"}, instance_id=instance_id, workspace=tmp_path))
    assert disabled["enabled"] is False
    with pytest.raises(agent_tools.ToolError, match="unknown tool"):
        _run(agent_tools.execute_tool("authored_tool", {}, workspace=tmp_path, instance_id=instance_id))

    enabled = json.loads(_exec("enable_plugin", {"name": "authored"}, instance_id=instance_id, workspace=tmp_path))
    assert enabled["enabled"] is True
    result = _run(agent_tools.execute_tool("authored_tool", {}, workspace=tmp_path, instance_id=instance_id))
    assert result == "hello from the authored plugin"


def test_remove_plugin(temp_db, tmp_path):
    instance_id = _create_instance()
    _exec("create_plugin", {"name": "authored", "code": PLUGIN_CODE}, instance_id=instance_id, workspace=tmp_path)

    assert "Removed" in _exec("remove_plugin", {"name": "authored"}, instance_id=instance_id, workspace=tmp_path)
    assert "No plugin" in _exec("remove_plugin", {"name": "authored"}, instance_id=instance_id, workspace=tmp_path)


def test_list_plugins(temp_db, tmp_path):
    instance_id = _create_instance()
    _exec("create_plugin", {"name": "authored", "code": PLUGIN_CODE}, instance_id=instance_id, workspace=tmp_path)

    listed = json.loads(_exec("list_plugins", {}, instance_id=instance_id, workspace=tmp_path))
    assert any(p["name"] == "authored" for p in listed)


def test_leaf_subagents_cannot_create_or_enable_plugins():
    from bot.agent_runtime.subagents import LEAF_BLOCKED_TOOLS

    assert "create_plugin" in LEAF_BLOCKED_TOOLS
    assert "enable_plugin" in LEAF_BLOCKED_TOOLS
