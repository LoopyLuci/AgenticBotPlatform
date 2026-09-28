"""Linked skill libraries (native_agent.skills.external_dirs, bot/skill_packs.py): another agent's library used in
place, at the size of a real one (a Hermes install on the development machine holds about 10,000 skills)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bot import skill_packs
from bot.agent_runtime import prompt, tools
from bot.config import config


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    skill_packs._linked.clear()
    monkeypatch.setattr(skill_packs, "user_root", lambda: tmp_path / "own-skills")


def skill(root: Path, rel: str, front: str) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{front}\n---\nDo the steps.\n", encoding="utf-8")
    return d


def link(*entries):
    config.set_values({("native_agent", "skills", "external_dirs"): list(entries)}, actor="test")


def test_a_library_is_read_in_place_nested_and_without_hidden_or_excluded_skills(tmp_path):
    lib = tmp_path / "lib"
    skill(lib, "top-skill", "name: top-skill\ndescription: at the top")
    skill(lib, "category/nested-skill", "name: nested-skill\ndescription: one level down")
    skill(lib, "a/b/deep-skill", "name: deep-skill\ndescription: two levels down")
    skill(lib, "a/b/c/too-deep", "name: too-deep\ndescription: three levels down")
    skill(lib, ".archive/archived", "name: archived\ndescription: hidden")
    skill(lib, "shipped", "name: shipped\ndescription: excluded")
    link({"path": str(lib), "exclude": ["shipped"]})
    found = skill_packs.discover(None)
    assert {"top-skill", "nested-skill", "deep-skill"} <= set(found)
    assert not {"too-deep", "archived", "shipped"} & set(found)
    assert found["nested-skill"].source == "linked"


def test_your_own_skills_win_over_a_library_and_a_library_over_a_project(tmp_path):
    lib, own, ws = tmp_path / "lib", tmp_path / "own-skills", tmp_path / "ws"
    skill(lib, "dup", "name: dup\ndescription: from the library")
    skill(ws / ".claude" / "skills", "dup", "name: dup\ndescription: from the project")
    link(str(lib))
    assert skill_packs.discover(ws)["dup"].description == "from the library"
    skill(own, "dup", "name: dup\ndescription: my own")
    assert skill_packs.discover(ws)["dup"].description == "my own"


def test_a_description_with_a_colon_is_read(tmp_path):
    """Strict YAML rejects `description: Affiliate: ...`; about one skill in a hundred in a real library is written so,
    and those skills used to be invisible to the model."""
    lib = tmp_path / "lib"
    skill(lib, "affiliate", "name: affiliate\ndescription: Affiliate: commissions, recruitment\nversion: 1\nmetadata:\n  tags: [a]")
    link(str(lib))
    assert skill_packs.discover(None)["affiliate"].description == "Affiliate: commissions, recruitment"


def test_a_multi_line_description_is_still_joined(tmp_path):
    lib = tmp_path / "lib"
    skill(lib, "wrapped", "name: wrapped\ndescription: first line\n  second line")
    skill(lib, "folded", "name: folded\ndescription: >-\n  folded\n  text")
    link(str(lib))
    found = skill_packs.discover(None)
    assert found["wrapped"].description == "first line second line" and found["folded"].description == "folded text"


def test_an_edited_skill_is_read_again(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    d = skill(lib, "changing", "name: changing\ndescription: before")
    link(str(lib))
    assert skill_packs.discover(None)["changing"].description == "before"
    (d / "SKILL.md").write_text("---\nname: changing\ndescription: after, longer\n---\nx\n", encoding="utf-8")
    monkeypatch.setattr(skill_packs, "LINKED_TTL_S", 0.0)
    skill_packs.discover(None)                     # starts the refresh in the background
    deadline = time.monotonic() + 10
    while skill_packs.discover(None)["changing"].description != "after, longer":
        assert time.monotonic() < deadline, "the edit was never picked up"
        time.sleep(0.05)


def test_bundled_files_are_listed_only_when_needed(tmp_path):
    lib = tmp_path / "lib"
    d = skill(lib, "with-files", "name: with-files\ndescription: has files")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)")
    link(str(lib))
    s = skill_packs.discover(None)["with-files"]
    assert str(d) not in skill_packs._files
    assert s.files == ("scripts/run.py",) and str(d) in skill_packs._files


def test_a_big_library_is_summarised_and_searchable(tmp_path):
    lib = tmp_path / "lib"
    for i in range(60):
        skill(lib, f"topic-{i:02d}", f"name: topic-{i:02d}\ndescription: notes on subject {i}")
    skill(lib, "android-gradle-workflow", "name: android-gradle-workflow\ndescription: Build Android apps with Gradle")
    link(str(lib))
    text = prompt.build(None, workspace=tmp_path)
    assert "... and 21 more" in text and "list_skills with a query" in text
    assert [s.name for s in skill_packs.search(None, "gradle android build")] == ["android-gradle-workflow"]
    assert skill_packs.search(None, "  ") == []


def test_list_skills_takes_a_query_and_caps_an_unfiltered_list(tmp_path, temp_db):
    lib = tmp_path / "lib"
    for i in range(120):
        skill(lib, f"topic-{i:03d}", f"name: topic-{i:03d}\ndescription: subject {i}")
    skill(lib, "gradle-tips", "name: gradle-tips\ndescription: Gradle build tips")
    link(str(lib))

    def call(inp):
        return json.loads(asyncio.run(tools.execute_tool("list_skills", inp, workspace=tmp_path, instance_id=1)))
    everything = call({})
    assert len([r for r in everything if r.get("kind") == "pack"]) == 100 and "21 more" in everything[-1]["note"]
    assert [r["name"] for r in call({"query": "gradle"})] == ["gradle-tips"]
    assert len(call({"query": "subject", "limit": 5})) == 5


def test_a_missing_library_is_harmless(tmp_path):
    link(str(tmp_path / "gone"), {"path": ""})
    assert skill_packs.discover(None) == {}
    assert skill_packs.linked_status() == [{"path": str(tmp_path / "gone"), "skills": 0, "loading": False}]


def test_the_first_load_does_not_hold_a_turn_forever(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    skill(lib, "slow", "name: slow\ndescription: x")
    link(str(lib))
    real = skill_packs._skill_folders
    monkeypatch.setattr(skill_packs, "_skill_folders", lambda root, depth: (time.sleep(1.5), real(root, depth))[1])
    monkeypatch.setattr(skill_packs, "FIRST_LOAD_WAIT_S", 0.2)
    started = time.monotonic()
    assert skill_packs.discover(None) == {}, "the turn goes ahead without the library while it loads"
    assert time.monotonic() - started < 1.0
    assert skill_packs.linked_status()[0]["loading"] is True
    deadline = time.monotonic() + 10
    while "slow" not in skill_packs.discover(None):
        assert time.monotonic() < deadline
        time.sleep(0.05)
