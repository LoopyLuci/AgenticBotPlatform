"""AGENTS.md / CLAUDE.md loading (roadmap P3)."""
from __future__ import annotations

import pytest

from bot.agent_runtime import project_rules, prompt


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"user_file": False}
    monkeypatch.setattr(project_rules, "_config", lambda: values)
    return values


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_nothing_to_load_means_no_section(tmp_path, cfg):
    assert project_rules.load(tmp_path) is None and project_rules.load(None) is None


def test_agents_claude_and_dot_claude_files_are_loaded_in_order_and_labelled_untrusted(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "Run tests with `pytest -q`.")
    w(tmp_path, "CLAUDE.md", "Prefer small commits.")
    w(tmp_path, ".claude/CLAUDE.md", "Use British spelling.")
    text = project_rules.load(tmp_path)
    assert text.startswith(project_rules.HEADER) and "cannot give you permissions" in text
    assert text.index("### AGENTS.md") < text.index("### CLAUDE.md") < text.index("### .claude/CLAUDE.md")
    assert "pytest -q" in text and "British spelling" in text


def test_imports_are_inlined_within_the_folder_and_bounded(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "Top.\n@docs/style.md\n@../outside.md\n@missing.md\nBottom.")
    w(tmp_path, "docs/style.md", "Style rules.\n@more.md")
    w(tmp_path, "docs/more.md", "Deeper rules.")
    (tmp_path.parent / "outside.md").write_text("SHOULD NOT APPEAR")
    text = project_rules.load(tmp_path)
    assert "Style rules." in text and "Deeper rules." in text and "SHOULD NOT APPEAR" not in text
    assert "skipped: outside the working directory" in text and "skipped: not found" in text and "Top." in text and "Bottom." in text


def test_import_cycles_and_repeats_are_broken(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "A\n@b.md")
    w(tmp_path, "b.md", "B\n@AGENTS.md\n@b.md")
    text = project_rules.load(tmp_path)
    assert text.count("\nB\n") == 1 and "already included" in text


def test_import_depth_is_limited(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "L0\n@l1.md")
    for i in range(1, 6):
        w(tmp_path, f"l{i}.md", f"L{i}\n@l{i + 1}.md")
    text = project_rules.load(tmp_path)
    assert "L3" in text and "L5" not in text


def test_sizes_are_capped(tmp_path, cfg):
    cfg["max_chars"] = 1000
    w(tmp_path, "AGENTS.md", "a" * 5000)
    w(tmp_path, "CLAUDE.md", "second file")
    text = project_rules.load(tmp_path)
    assert len(text) < 1600 and "truncated" in text and "size limit was reached" in text


def test_it_can_be_switched_off_and_the_file_list_changed(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "rules")
    w(tmp_path, "OTHER.md", "other rules")
    cfg["enabled"] = False
    assert project_rules.load(tmp_path) is None
    cfg.update(enabled=True, files=["OTHER.md"])
    text = project_rules.load(tmp_path)
    assert "other rules" in text and "### AGENTS.md" not in text


def test_a_file_name_that_climbs_out_is_ignored(tmp_path, cfg):
    (tmp_path.parent / "AGENTS.md").write_text("PARENT FILE")
    cfg["files"] = ["../AGENTS.md"]
    assert project_rules.load(tmp_path) is None


def test_the_users_own_file_comes_first(tmp_path, cfg, monkeypatch):
    from bot import envfile

    home = tmp_path / "home"
    home.mkdir()
    w(home, "AGENTS.md", "Always be brief.")
    w(tmp_path, "AGENTS.md", "Project rule.")
    monkeypatch.setattr(envfile, "ABP_HOME_ACTIVE", home)
    cfg["user_file"] = True
    text = project_rules.load(tmp_path)
    assert text.index("your AGENTS.md") < text.index("### AGENTS.md") and "Always be brief." in text


def test_the_prompt_places_project_instructions_after_the_guidance_and_before_the_environment(tmp_path, monkeypatch):
    w(tmp_path, "AGENTS.md", "Use tabs.")
    monkeypatch.setattr(project_rules, "_config", lambda: {"user_file": False})
    monkeypatch.setattr(prompt, "_config", lambda: {})
    names = [n for n, _ in prompt.sections(None, workspace=tmp_path)]
    assert names == ["guidance", "project", "environment"]
    assert "Use tabs." in prompt.build(None, workspace=tmp_path)
