"""New agent_runtime tools for manager/worker coordination:
get_my_profile, update_agent_config, read/write/list_project_context —
plus bot.bot_instances.render_profile_markdown, which get_my_profile and
the /api/bots/{id}/profile route both wrap.
"""

from __future__ import annotations

import asyncio

from bot import bot_instances
from bot.agent_runtime import tools as agent_tools
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name, **overrides):
    kwargs = dict(
        name=name, platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


# --------------------------------------------------------- render_profile_markdown


def test_render_profile_markdown_includes_all_fields(temp_db):
    target_id = _create_instance("worker-a")
    manager_id = _create_instance(
        "manager", persona="manager", model="claude-opus-5",
        custom_instructions="Coordinate the swarm.", can_target=[target_id],
    )

    markdown = bot_instances.render_profile_markdown(manager_id)

    assert "# Agent profile: manager" in markdown
    assert f"**ID:** {manager_id}" in markdown
    assert "**Persona:** manager" in markdown
    assert "**Model override:** claude-opus-5" in markdown
    assert f"worker-a (id {target_id})" in markdown
    assert "Coordinate the swarm." in markdown


def test_render_profile_markdown_handles_defaults_honestly(temp_db):
    instance_id = _create_instance("plain-worker")

    markdown = bot_instances.render_profile_markdown(instance_id)

    assert "(backend default)" in markdown
    assert "(none)" in markdown  # can_target
    assert "(none set)" in markdown  # custom_instructions


def test_render_profile_markdown_returns_none_for_missing_instance(temp_db):
    assert bot_instances.render_profile_markdown(999999) is None


# --------------------------------------------------------------- get_my_profile


def test_get_my_profile_returns_own_markdown(temp_db, tmp_path):
    instance_id = _create_instance("self-aware-bot")

    result = _run(agent_tools.execute_tool("get_my_profile", {}, workspace=tmp_path, instance_id=instance_id))

    assert "self-aware-bot" in result


def test_get_my_profile_requires_instance_context(temp_db, tmp_path):
    try:
        _run(agent_tools.execute_tool("get_my_profile", {}, workspace=tmp_path, instance_id=None))
        assert False, "expected ToolError"
    except agent_tools.ToolError:
        pass


# ---------------------------------------------------------- update_agent_config


def test_update_agent_config_renames_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    manager_id = _create_instance("manager")
    worker_id = _create_instance("worker-old-name")

    result = _run(agent_tools.execute_tool(
        "update_agent_config", {"target_instance": str(worker_id), "name": "worker-new-name"},
        workspace=tmp_path, instance_id=manager_id,
    ))

    assert "Updated 'worker-old-name'" in result
    assert bot_instances.get_instance(worker_id)["name"] == "worker-new-name"


def test_update_agent_config_updates_can_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    manager_id = _create_instance("manager")
    worker_id = _create_instance("worker")
    other_id = _create_instance("other")

    _run(agent_tools.execute_tool(
        "update_agent_config", {"target_instance": str(worker_id), "can_target": [other_id]},
        workspace=tmp_path, instance_id=manager_id,
    ))

    assert bot_instances.get_instance(worker_id)["can_target"] == [other_id]


def test_update_agent_config_blocked_by_allowlist(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "allowlist"}, "agent_runtime": {}})
    manager_id = _create_instance("manager", can_target=[])
    worker_id = _create_instance("worker")

    try:
        _run(agent_tools.execute_tool(
            "update_agent_config", {"target_instance": str(worker_id), "name": "hacked"},
            workspace=tmp_path, instance_id=manager_id,
        ))
        assert False, "expected ToolError"
    except agent_tools.ToolError as exc:
        assert "not permitted" in str(exc)
    assert bot_instances.get_instance(worker_id)["name"] == "worker"  # untouched


def test_update_agent_config_allowed_when_target_in_can_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "allowlist"}, "agent_runtime": {}})
    worker_id = _create_instance("worker")
    manager_id = _create_instance("manager", can_target=[worker_id])

    _run(agent_tools.execute_tool(
        "update_agent_config", {"target_instance": str(worker_id), "persona": "manager"},
        workspace=tmp_path, instance_id=manager_id,
    ))

    assert bot_instances.get_instance(worker_id)["persona"] == "manager"


def test_update_agent_config_requires_at_least_one_field(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    manager_id = _create_instance("manager")
    worker_id = _create_instance("worker")

    try:
        _run(agent_tools.execute_tool(
            "update_agent_config", {"target_instance": str(worker_id)},
            workspace=tmp_path, instance_id=manager_id,
        ))
        assert False, "expected ToolError"
    except agent_tools.ToolError as exc:
        assert "nothing to update" in str(exc)


def test_update_agent_config_is_dangerous():
    assert agent_tools.is_dangerous("update_agent_config") is True


# ------------------------------------------------------------ project context


def test_write_then_read_project_context(temp_db, tmp_path):
    instance_id = _bot_instance_id(tmp_path)

    _run(agent_tools.execute_tool(
        "write_project_context", {"name": "status", "content": "# Status\nAll green."},
        workspace=tmp_path, instance_id=instance_id,
    ))
    result = _run(agent_tools.execute_tool(
        "read_project_context", {"name": "status"}, workspace=tmp_path, instance_id=instance_id,
    ))

    assert result == "# Status\nAll green."


def test_read_missing_project_context_is_a_helpful_message_not_an_error(temp_db, tmp_path):
    instance_id = _bot_instance_id(tmp_path)

    result = _run(agent_tools.execute_tool(
        "read_project_context", {"name": "does-not-exist"}, workspace=tmp_path, instance_id=instance_id,
    ))

    assert "No shared context doc" in result


def test_list_project_context_shows_every_doc(temp_db, tmp_path):
    instance_id = _bot_instance_id(tmp_path)

    _run(agent_tools.execute_tool(
        "write_project_context", {"name": "status", "content": "hi"}, workspace=tmp_path, instance_id=instance_id,
    ))
    _run(agent_tools.execute_tool(
        "write_project_context", {"name": "notes", "content": "there"}, workspace=tmp_path, instance_id=instance_id,
    ))

    result = _run(agent_tools.execute_tool("list_project_context", {}, workspace=tmp_path, instance_id=instance_id))

    assert "status" in result
    assert "notes" in result


def test_write_project_context_is_not_dangerous():
    # Consistent with save_memory's precedent — shared state, but not
    # gated behind human approval the way run_shell/write_file/
    # update_agent_config are.
    assert agent_tools.is_dangerous("write_project_context") is False


def _bot_instance_id(tmp_path):
    return _create_instance("ctx-bot")
