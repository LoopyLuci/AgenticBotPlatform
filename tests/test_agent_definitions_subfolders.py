"""Agent definitions in sub-folders of the agent folders: naming, discovery, collisions."""
from __future__ import annotations

import logging

import pytest

from bot.agent_runtime import agent_defs


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    home = tmp_path / "abp-data"
    home.mkdir()
    monkeypatch.setattr(agent_defs, "_data_dir", lambda: home)
    return home


def write(root, rel, front="description: d", body="Do the thing."):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}\n")
    return path


def test_a_file_in_a_subfolder_is_named_by_its_path(tmp_path):
    write(tmp_path, ".abp/agents/review/security.md")
    write(tmp_path, ".claude/agents/ops/deploy/check.md")
    found = agent_defs.discover(tmp_path)
    assert found["review/security"].source == "project" and found["review/security"].origin.endswith("security.md")
    assert "ops/deploy/check" in found
    assert agent_defs.resolve("Review/Security", tmp_path) is not None


def test_a_name_in_the_front_matter_wins_over_the_path(tmp_path):
    write(tmp_path, ".abp/agents/review/security.md", "name: sec-review\ndescription: d")
    found = agent_defs.discover(tmp_path)
    assert "sec-review" in found and "review/security" not in found


def test_the_top_level_still_works_and_other_files_are_ignored(tmp_path):
    write(tmp_path, ".abp/agents/top.md")
    (tmp_path / ".abp/agents/notes.txt").write_text("x")
    write(tmp_path, ".abp/agents/review/readme.txt")
    assert {"top"} <= set(agent_defs.discover(tmp_path)) and "review/readme" not in agent_defs.discover(tmp_path)


def test_user_folder_subfolders_work_and_beat_a_project_of_the_same_name(tmp_path, _data):
    write(tmp_path, ".abp/agents/review/security.md", "description: project version")
    write(_data / "agents", "review/security.md", "description: user version")
    assert agent_defs.resolve("review/security", tmp_path).description == "user version"
    assert agent_defs.resolve("review/security", tmp_path).source == "user"


def test_a_project_cannot_shadow_a_builtin_through_a_subfolder_name(tmp_path):
    write(tmp_path, ".abp/agents/explore.md", "description: hijack")
    assert agent_defs.discover(tmp_path)["explore"].source == "built-in"


def test_two_files_with_the_same_name_the_later_one_wins_and_it_is_logged(tmp_path, caplog):
    write(tmp_path, ".abp/agents/security.md", "description: top level")
    write(tmp_path, ".abp/agents/review/other.md", "name: security\ndescription: nested")
    with caplog.at_level(logging.WARNING, logger="bot.agent_runtime.agent_defs"):
        found = agent_defs.discover(tmp_path)
    # sorted by name: "review" sorts before "security.md", so the top-level file is the later one
    assert found["security"].description == "top level"
    assert any("defined by both" in r.message for r in caplog.records)


def test_same_stem_in_different_folders_do_not_collide(tmp_path):
    write(tmp_path, ".abp/agents/a/check.md", "description: a")
    write(tmp_path, ".abp/agents/b/check.md", "description: b")
    found = agent_defs.discover(tmp_path)
    assert found["a/check"].description == "a" and found["b/check"].description == "b"


def test_depth_hidden_folders_and_bad_names_are_limited(tmp_path):
    write(tmp_path, ".abp/agents/a/b/c/ok.md")              # three folders deep: found
    write(tmp_path, ".abp/agents/a/b/c/d/too-deep.md")
    write(tmp_path, ".abp/agents/.hidden/secret.md")
    write(tmp_path, ".abp/agents/Bad Folder/x.md")
    found = agent_defs.discover(tmp_path)
    assert "a/b/c/ok" in found
    assert not any("too-deep" in n or "secret" in n or "bad" in n for n in found)


def test_list_agents_shows_the_nested_name(tmp_path):
    import asyncio
    import json

    from bot.agent_runtime import tools

    write(tmp_path, ".abp/agents/review/security.md")
    rows = json.loads(asyncio.run(tools.execute_tool("list_agents", {}, workspace=tmp_path)))
    assert any(r["name"] == "review/security" for r in rows)
