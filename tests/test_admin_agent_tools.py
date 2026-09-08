"""The admin control surface's agent-runtime tools (bot/agent_runtime/tools.py's
ADMIN_TOOLS_STANDARD/ADMIN_TOOLS_ELEVATED) — gated to the one instance
flagged agent_settings.is_admin_instance=True (defense-in-depth via
_require_admin, independent of native_backend.py's schema filter), with
ADMIN_TOOLS_ELEVATED additionally requiring an elevated-or-higher
device_tier. See docs/adr/0008-single-instance-admin-tool-gate.md.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import agent_settings, bot_instances, db
from bot.agent_runtime import tools as agent_tools


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name, is_admin=False, **overrides):
    kwargs = dict(
        name=name, platform="telegram", backend="native_agent",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    instance_id = bot_instances.create_instance(**kwargs)
    if is_admin:
        agent_settings.set_settings(instance_id, is_admin_instance=True)
    return instance_id


@pytest.mark.parametrize("name", sorted(agent_tools.ADMIN_TOOLS_STANDARD))
def test_admin_tool_blocked_for_non_admin_instance(temp_db, tmp_path, name):
    instance_id = _create_instance("ordinary-bot")
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(name, {}, workspace=tmp_path, instance_id=instance_id))


@pytest.mark.parametrize("name", sorted(agent_tools.ADMIN_TOOLS_STANDARD))
def test_admin_tool_blocked_with_no_instance_context(temp_db, tmp_path, name):
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(name, {}, workspace=tmp_path, instance_id=None))


@pytest.mark.parametrize("name", sorted(agent_tools.ADMIN_TOOLS_ELEVATED))
def test_elevated_tool_blocked_without_elevated_device_tier(temp_db, tmp_path, name):
    admin_id = _create_instance("admin-bot", is_admin=True)
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(name, {}, workspace=tmp_path, instance_id=admin_id, device_tier="standard"))


def test_admin_tools_are_dangerous_except_reads():
    read_only = {
        "admin_list_bot_instances", "admin_get_bot_instance", "admin_get_agent_settings",
        "admin_get_auto_manage_config", "admin_get_estop_status", "admin_list_hooks", "admin_list_devices",
    }
    for name in agent_tools.ADMIN_TOOLS:
        expected = name not in read_only
        assert agent_tools.is_dangerous(name) is expected, name


def test_admin_list_bot_instances_redacts_credentials(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)

    result = _run(agent_tools.execute_tool("admin_list_bot_instances", {}, workspace=tmp_path, instance_id=admin_id))

    rows = json.loads(result)
    admin_row = next(r for r in rows if r["id"] == admin_id)
    assert "123456789:AAExampleTokenFromBotFather1234" not in json.dumps(admin_row)
    assert admin_row["credentials"]["bot_token"].endswith("1234")
    assert admin_row["credentials"]["bot_token"].startswith("...")


def test_admin_create_bot_instance_creates_row_and_redacts_response(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)

    result = _run(agent_tools.execute_tool(
        "admin_create_bot_instance",
        {
            "name": "new-worker", "platform": "telegram", "backend": "api",
            "credentials": {"bot_token": "987654321:AAExampleTokenFromBotFather5678"},
            "allowed_user_ids": [222],
        },
        workspace=tmp_path, instance_id=admin_id,
    ))

    assert "987654321:AAExampleTokenFromBotFather5678" not in result
    new_row = bot_instances.list_instances(platform="telegram")
    assert any(r["name"] == "new-worker" for r in new_row)


def test_admin_delete_bot_instance_refuses_self_delete(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)

    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "admin_delete_bot_instance", {"target_instance": str(admin_id), "confirm": True},
            workspace=tmp_path, instance_id=admin_id,
        ))


def test_admin_delete_bot_instance_requires_confirm_flag(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    target_id = _create_instance("throwaway-worker")

    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "admin_delete_bot_instance", {"target_instance": str(target_id)},
            workspace=tmp_path, instance_id=admin_id,
        ))
    assert bot_instances.get_instance(target_id) is not None


def test_admin_delete_bot_instance_succeeds_with_confirm(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    target_id = _create_instance("throwaway-worker")

    _run(agent_tools.execute_tool(
        "admin_delete_bot_instance", {"target_instance": str(target_id), "confirm": True},
        workspace=tmp_path, instance_id=admin_id,
    ))

    assert bot_instances.get_instance(target_id) is None


def test_admin_set_agent_settings_rejects_is_admin_instance_field(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    target_id = _create_instance("target-worker")

    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "admin_set_agent_settings",
            {"target_instance": str(target_id), "fields": {"is_admin_instance": True}},
            workspace=tmp_path, instance_id=admin_id,
        ))
    assert agent_settings.get(target_id)["is_admin_instance"] == False  # noqa: E712


def test_admin_set_agent_settings_hides_is_admin_instance_on_read(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)

    result = _run(agent_tools.execute_tool(
        "admin_get_agent_settings", {"target_instance": str(admin_id)}, workspace=tmp_path, instance_id=admin_id,
    ))

    assert json.loads(result)["is_admin_instance"] == "(hidden — dashboard/MCP only)"


def test_admin_set_default_backend_updates_config(temp_db, tmp_path):
    """Redirects the config singleton's path/data to a scratch copy for
    the duration of this test — set_value() does a real atomic
    write-then-replace to disk and reassigns config._data in place, so
    monkeypatch's own undo (which only tracks setattr calls, not later
    in-place reassignment by reload()/set_value()) can't be relied on to
    restore it; this must save and restore by hand, in a finally block,
    to guarantee the real config/backends.yaml and the in-memory
    singleton are never left corrupted for later tests in this process."""
    from bot.config import config

    original_path, original_data, original_version = config.path, config._data, config.version
    scratch = tmp_path / "backends.yaml"
    scratch.write_text("default_backend: cli\n", encoding="utf-8")
    config.path = scratch
    config.reload()
    try:
        admin_id = _create_instance("admin-bot", is_admin=True)

        _run(agent_tools.execute_tool(
            "admin_set_default_backend", {"backend": "custom_model"}, workspace=tmp_path / "ws", instance_id=admin_id,
        ))

        assert config.current.get("default_backend") == "custom_model"
    finally:
        config.path, config._data, config.version = original_path, original_data, original_version


def test_admin_engage_and_disengage_estop(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    from bot.agent_runtime import estop

    _run(agent_tools.execute_tool("admin_engage_estop", {"reason": "testing"}, workspace=tmp_path, instance_id=admin_id))
    assert estop.is_engaged()

    _run(agent_tools.execute_tool("admin_disengage_estop", {}, workspace=tmp_path, instance_id=admin_id))
    assert not estop.is_engaged()


def test_admin_add_list_enable_disable_remove_hook_roundtrip(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)

    _run(agent_tools.execute_tool(
        "admin_add_hook", {"event": "PreToolUse", "command": "echo hi"}, workspace=tmp_path, instance_id=admin_id,
    ))
    hooks = json.loads(_run(agent_tools.execute_tool("admin_list_hooks", {}, workspace=tmp_path, instance_id=admin_id)))
    assert len(hooks) == 1
    hook_id = hooks[0]["id"]

    _run(agent_tools.execute_tool("admin_disable_hook", {"hook_id": hook_id}, workspace=tmp_path, instance_id=admin_id))
    assert db.get_agent_hook(hook_id)["enabled"] == 0

    _run(agent_tools.execute_tool("admin_enable_hook", {"hook_id": hook_id}, workspace=tmp_path, instance_id=admin_id))
    assert db.get_agent_hook(hook_id)["enabled"] == 1

    _run(agent_tools.execute_tool("admin_remove_hook", {"hook_id": hook_id}, workspace=tmp_path, instance_id=admin_id))
    assert db.get_agent_hook(hook_id) is None


def test_admin_list_devices_works_with_elevated_device_tier(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    db.create_api_key("some-phone", permission_tier="standard")

    result = _run(agent_tools.execute_tool(
        "admin_list_devices", {}, workspace=tmp_path, instance_id=admin_id, device_tier="elevated",
    ))

    assert "some-phone" in result


def test_admin_set_device_tier_refuses_peer_or_superior(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    peer_id, _ = db.create_api_key("peer-phone", permission_tier="elevated")

    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "admin_set_device_tier", {"key_id": peer_id, "tier": "none"},
            workspace=tmp_path, instance_id=admin_id, device_tier="elevated",
        ))


def test_admin_set_device_tier_allows_strictly_lower(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    low_id, _ = db.create_api_key("low-phone", permission_tier="none")

    _run(agent_tools.execute_tool(
        "admin_set_device_tier", {"key_id": low_id, "tier": "standard"},
        workspace=tmp_path, instance_id=admin_id, device_tier="unrestricted",
    ))

    assert db.get_api_key(low_id)["permission_tier"] == "standard"


def test_admin_revoke_device_refuses_peer_or_superior(temp_db, tmp_path):
    admin_id = _create_instance("admin-bot", is_admin=True)
    peer_id, _ = db.create_api_key("peer-phone", permission_tier="unrestricted")

    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "admin_revoke_device", {"key_id": peer_id},
            workspace=tmp_path, instance_id=admin_id, device_tier="elevated",
        ))


def test_execute_tool_defense_in_depth_bypassing_schema_filter(temp_db, tmp_path):
    """Calling execute_tool() directly for a non-admin instance_id, as if
    the schema filter had somehow been bypassed — _require_admin must
    still refuse."""
    ordinary_id = _create_instance("ordinary-bot")
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool("admin_list_bot_instances", {}, workspace=tmp_path, instance_id=ordinary_id))
