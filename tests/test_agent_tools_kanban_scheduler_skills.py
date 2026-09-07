"""Thin agent-tool wrappers around bot/kanban.py, bot/scheduler.py, and
bot/skills.py — closing the gap this session's live audit found: those
subsystems were real and working but reachable only from the dashboard/
slash-commands, not from an api/native_agent-backed agent's own
tool-calling loop.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot import bot_instances, db
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


# ------------------------------------------------------------------ kanban


def test_kanban_add_and_list_cards(temp_db, tmp_path):
    instance_id = _create_instance()

    added = json.loads(_exec(
        "kanban_add_card", {"board": "default", "column": "todo", "text": "write tests"},
        instance_id=instance_id, workspace=tmp_path,
    ))
    assert added["column_name"] == "todo"

    cards = json.loads(_exec(
        "kanban_list_cards", {"board": "default"}, instance_id=instance_id, workspace=tmp_path,
    ))
    assert len(cards) == 1
    assert cards[0]["text"] == "write tests"


def test_kanban_move_card(temp_db, tmp_path):
    instance_id = _create_instance()
    added = json.loads(_exec(
        "kanban_add_card", {"board": "default", "column": "todo", "text": "x"},
        instance_id=instance_id, workspace=tmp_path,
    ))

    moved = json.loads(_exec(
        "kanban_move_card", {"card_id": added["id"], "column": "done"},
        instance_id=instance_id, workspace=tmp_path,
    ))
    assert moved["column_name"] == "done"


def test_kanban_move_card_rejects_unknown_card(temp_db, tmp_path):
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError):
        _exec("kanban_move_card", {"card_id": 999999, "column": "done"}, instance_id=instance_id, workspace=tmp_path)


# --------------------------------------------------------------- scheduler


def test_schedule_command_create_list_pause_remove(temp_db, tmp_path):
    instance_id = _create_instance()

    created = json.loads(_exec(
        "schedule_command", {"kind": "status_check", "prompt": "check status", "interval": "30s"},
        instance_id=instance_id, workspace=tmp_path,
    ))
    sched_id = created["id"]
    assert sched_id

    listed = json.loads(_exec("list_schedules", {}, instance_id=instance_id, workspace=tmp_path))
    assert len(listed) == 1
    assert listed[0]["id"] == sched_id

    _exec("pause_schedule", {"schedule_id": sched_id}, instance_id=instance_id, workspace=tmp_path)
    assert db.get_scheduled_command(sched_id)["enabled"] == 0

    _exec("remove_schedule", {"schedule_id": sched_id}, instance_id=instance_id, workspace=tmp_path)
    assert db.get_scheduled_command(sched_id) is None


def test_schedule_command_rejects_bad_interval(temp_db, tmp_path):
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError):
        _exec(
            "schedule_command", {"kind": "x", "prompt": "x", "interval": "not-a-duration"},
            instance_id=instance_id, workspace=tmp_path,
        )


def test_pause_schedule_refuses_another_instances_schedule(temp_db, tmp_path):
    owner_id = _create_instance()
    other_id = bot_instances.create_instance(
        name="other", platform="telegram", backend="api",
        credentials={"bot_token": "987654321:BBExampleTokenFromBotFather1234"},
        allowed_user_ids=[222], enabled=False,
    )
    created = json.loads(_exec(
        "schedule_command", {"kind": "x", "prompt": "x", "interval": "30s"},
        instance_id=owner_id, workspace=tmp_path,
    ))

    with pytest.raises(agent_tools.ToolError, match="not found"):
        _exec("pause_schedule", {"schedule_id": created["id"]}, instance_id=other_id, workspace=tmp_path)


# ------------------------------------------------------------------ skills


def test_install_skill_from_within_workspace(temp_db, tmp_path):
    instance_id = _create_instance()
    skill_file = tmp_path / "my_skill.md"
    skill_file.write_text("A short description\n\nThe rest of the content.", encoding="utf-8")

    installed = json.loads(_exec(
        "install_skill", {"path": "my_skill.md"}, instance_id=instance_id, workspace=tmp_path,
    ))
    assert installed["name"] == "my_skill"
    assert installed["description"] == "A short description"

    listed = json.loads(_exec("list_skills", {}, instance_id=instance_id, workspace=tmp_path))
    assert any(s["name"] == "my_skill" for s in listed)


def test_install_skill_refuses_path_outside_workspace(temp_db, tmp_path, tmp_path_factory):
    instance_id = _create_instance()
    outside_dir = tmp_path_factory.mktemp("outside")
    outside_file = outside_dir / "secret.md"
    outside_file.write_text("shouldn't be readable this way", encoding="utf-8")

    with pytest.raises(agent_tools.ToolError, match="outside the working directory"):
        _exec(
            "install_skill", {"path": str(outside_file)}, instance_id=instance_id, workspace=tmp_path,
        )
