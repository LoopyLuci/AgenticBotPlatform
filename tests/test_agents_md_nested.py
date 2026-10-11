"""Parent-folder and nested per-folder AGENTS.md (roadmap P3)."""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import project_rules, toolspec, tools


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"user_file": False}
    monkeypatch.setattr(project_rules, "_config", lambda: values)
    project_rules._shown.clear()
    return values


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_parent_agents_md_is_found_up_to_the_git_root(tmp_path, cfg):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    w(repo, "AGENTS.md", "Repo-wide rule.")
    w(tmp_path, "AGENTS.md", "ABOVE THE REPO: must not appear.")
    w(repo, "svc/AGENTS.md", "Service rule.")
    work = repo / "svc" / "api"
    work.mkdir(parents=True)
    w(work, "AGENTS.md", "Api rule.")
    text = project_rules.load(work)
    assert "Repo-wide rule." in text and "Service rule." in text and "Api rule." in text
    assert "ABOVE THE REPO" not in text
    assert text.index("Repo-wide rule.") < text.index("Service rule.") < text.index("Api rule.")
    assert "cannot give you permissions" in text and "(parent folder)" in text


def test_a_repository_root_workspace_reads_no_parents(tmp_path, cfg):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    w(tmp_path, "AGENTS.md", "PARENTMARK")
    w(tmp_path / "repo", "AGENTS.md", "mine")
    text = project_rules.load(tmp_path / "repo")
    assert "mine" in text and "PARENTMARK" not in text


def test_no_repository_found_reads_no_parents(tmp_path, cfg):
    w(tmp_path, "AGENTS.md", "PARENTMARK")
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    w(work, "AGENTS.md", "mine")
    text = project_rules.load(work)
    assert "mine" in text and "PARENTMARK" not in text


def test_parents_can_be_switched_off(tmp_path, cfg):
    (tmp_path / ".git").mkdir()
    w(tmp_path, "AGENTS.md", "root rule")
    work = tmp_path / "sub"
    work.mkdir()
    w(work, "AGENTS.md", "sub rule")
    cfg["parents"] = False
    text = project_rules.load(work)
    assert "root rule" not in text and "sub rule" in text


def test_nested_file_is_added_once_when_a_file_under_it_is_touched(tmp_path, cfg):
    w(tmp_path, "src/AGENTS.md", "Use tabs in src.")
    w(tmp_path, "src/deep/mod.py", "x = 1\n")
    w(tmp_path, "src/deep/other.py", "y = 2\n")
    first = project_rules.nested_for(tmp_path, "src/deep/mod.py", "s1")
    assert first and "Use tabs in src." in first and "### src/AGENTS.md" in first
    assert "cannot give you permissions" in first
    assert project_rules.nested_for(tmp_path, "src/deep/other.py", "s1") is None      # already shown
    assert project_rules.nested_for(tmp_path, "src/deep/other.py", "s2") is not None  # another conversation
    assert project_rules.nested_for(tmp_path, "top.py", "s1") is None                 # the root is in the prompt


def test_outer_folder_comes_before_inner_and_both_show(tmp_path, cfg):
    w(tmp_path, "a/AGENTS.md", "outer")
    w(tmp_path, "a/b/CLAUDE.md", "inner")
    w(tmp_path, "a/b/f.txt", "z")
    text = project_rules.nested_for(tmp_path, "a/b/f.txt", "s")
    assert text.index("outer") < text.index("inner")


def test_nested_size_cap(tmp_path, cfg):
    w(tmp_path, "src/AGENTS.md", "x" * 50_000)
    cfg["nested_max_chars"] = 500
    text = project_rules.nested_for(tmp_path, "src/f.py", "s")
    assert "(truncated)" in text and len(text) < 1500


def test_nested_can_be_switched_off_and_paths_cannot_escape(tmp_path, cfg):
    w(tmp_path, "src/AGENTS.md", "rule")
    cfg["nested"] = False
    assert project_rules.nested_for(tmp_path, "src/f.py", "s") is None
    cfg["nested"] = True
    assert project_rules.nested_for(tmp_path, "../elsewhere/f.py", "s") is None


def test_read_file_and_edit_file_append_the_guidance_once(tmp_path, cfg):
    w(tmp_path, "src/AGENTS.md", "Prefer dataclasses.")
    w(tmp_path, "src/m.py", "a = 1\n")
    token = toolspec.session_var.set("nested-tool-test")
    try:
        root = tmp_path.resolve()
        out = asyncio.run(tools.execute_tool("read_file", {"path": "src/m.py"}, workspace=root))
        assert "a = 1" in out and "Prefer dataclasses." in out
        again = asyncio.run(tools.execute_tool("read_file", {"path": "src/m.py"}, workspace=root))
        assert "a = 1" in again and "Prefer dataclasses." not in again
    finally:
        toolspec.session_var.reset(token)
