"""Editing, search and planning tools (roadmap P1)."""
from __future__ import annotations

import asyncio
import json

import pytest

from bot.agent_runtime import coding_tools, toolspec, tools
from bot.agent_runtime.errors import ToolError


@pytest.fixture(autouse=True)
def _session():
    coding_tools.forget_reads()
    token = toolspec.session_var.set("test-session")
    yield
    toolspec.session_var.reset(token)
    coding_tools.forget_reads()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def call(ws, name, **inp):
    return asyncio.run(tools.execute_tool(name, inp, workspace=ws))


def fails(ws, name, match, **inp):
    with pytest.raises(ToolError, match=match):
        call(ws, name, **inp)


def put(ws, rel, text, *, read=True):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    if read:
        call(ws, "read_file", path=rel)
    return p


# ---- registration and approval ---------------------------------------------------
def test_tools_are_offered_and_gated_correctly():
    names = {s["name"] for s in tools.all_tool_schemas()}
    assert {"edit_file", "multi_edit", "apply_patch", "grep", "glob", "todo_write", "todo_read"} <= names
    for n in ("edit_file", "multi_edit", "apply_patch"):
        assert tools.is_dangerous(n), n
    for n in ("grep", "glob", "todo_read", "todo_write"):
        assert not tools.is_dangerous(n), n
    assert toolspec.is_concurrency_safe("grep") and toolspec.is_concurrency_safe("glob")


# ---- edit_file -------------------------------------------------------------------
def test_edit_replaces_one_exact_match_and_shows_a_diff(ws):
    put(ws, "a.py", "x = 1\ny = 2\nz = 3\n")
    out = call(ws, "edit_file", path="a.py", old_string="y = 2", new_string="y = 20")
    assert "1 replacement" in out and "-y = 2" in out and "+y = 20" in out
    assert (ws / "a.py").read_text() == "x = 1\ny = 20\nz = 3\n"


def test_edit_requires_the_file_to_have_been_read(ws):
    put(ws, "a.txt", "hello\n", read=False)
    fails(ws, "edit_file", "read a.txt with read_file", path="a.txt", old_string="hello", new_string="bye")
    assert (ws / "a.txt").read_text() == "hello\n"


def test_edit_notices_a_change_made_after_the_read(ws):
    p = put(ws, "a.txt", "hello\n")
    p.write_text("hello there\n")
    fails(ws, "edit_file", "changed on disk", path="a.txt", old_string="hello", new_string="bye")


def test_reads_in_one_session_do_not_count_in_another(ws):
    put(ws, "a.txt", "hello\n")
    other = toolspec.session_var.set("another-session")
    try:
        fails(ws, "edit_file", "read a.txt", path="a.txt", old_string="hello", new_string="bye")
    finally:
        toolspec.session_var.reset(other)


def test_read_first_rule_can_be_switched_off(ws, monkeypatch):
    put(ws, "a.txt", "hello\n", read=False)
    monkeypatch.setattr(coding_tools, "_require_read_first", lambda: False)
    call(ws, "edit_file", path="a.txt", old_string="hello", new_string="bye")
    assert (ws / "a.txt").read_text() == "bye\n"


def test_edit_refuses_an_ambiguous_match_and_replace_all_changes_every_one(ws):
    put(ws, "a.txt", "cat\ndog\ncat\n")
    fails(ws, "edit_file", "matches 2 places", path="a.txt", old_string="cat", new_string="bird")
    out = call(ws, "edit_file", path="a.txt", old_string="cat", new_string="bird", replace_all=True)
    assert "2 replacements" in out and (ws / "a.txt").read_text() == "bird\ndog\nbird\n"


def test_edit_accepts_a_unique_match_that_differs_only_in_indentation_and_reindents(ws):
    put(ws, "a.py", "def f():\n        if x:\n            return 1\n")
    out = call(ws, "edit_file", path="a.py", old_string="if x:\n    return 1", new_string="if x:\n    return 2")
    assert "whitespace-insensitive" in out
    assert (ws / "a.py").read_text() == "def f():\n        if x:\n            return 2\n"


def test_fuzzy_match_is_refused_when_it_is_ambiguous(ws):
    put(ws, "a.txt", "  foo\n  bar\n    foo\n    bar\n")
    fails(ws, "edit_file", "once whitespace is ignored", path="a.txt", old_string="foo\nbar", new_string="x")


def test_edit_not_found_points_at_the_closest_line(ws):
    put(ws, "a.txt", "alpha = 1\nbeta = 2\n")
    fails(ws, "edit_file", "closest line is 2", path="a.txt", old_string="beta = 3", new_string="beta = 4")


def test_edit_keeps_windows_line_endings(ws):
    p = put(ws, "a.txt", "one\r\ntwo\r\nthree\r\n")
    call(ws, "edit_file", path="a.txt", old_string="two", new_string="TWO")
    assert p.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_edit_can_create_a_file_with_an_empty_old_string(ws):
    out = call(ws, "edit_file", path="new/dir/n.txt", old_string="", new_string="fresh")
    assert "Created" in out and (ws / "new" / "dir" / "n.txt").read_text() == "fresh"


def test_edit_rejects_identical_text_binary_files_and_paths_outside_the_workspace(ws):
    put(ws, "a.txt", "same\n")
    fails(ws, "edit_file", "identical", path="a.txt", old_string="same", new_string="same")
    (ws / "b.bin").write_bytes(b"\x00\x01\x02 data")
    call(ws, "read_file", path="b.bin")
    fails(ws, "edit_file", "binary", path="b.bin", old_string="data", new_string="x")
    fails(ws, "edit_file", "outside the working directory", path="../evil.txt", old_string="", new_string="x")


# ---- multi_edit ------------------------------------------------------------------
def test_multi_edit_applies_in_order(ws):
    put(ws, "a.txt", "one two three\n")
    call(ws, "multi_edit", path="a.txt", edits=[
        {"old_string": "one", "new_string": "1"}, {"old_string": "1 two", "new_string": "1 2"}])
    assert (ws / "a.txt").read_text() == "1 2 three\n"


def test_multi_edit_is_all_or_nothing(ws):
    put(ws, "a.txt", "one two three\n")
    fails(ws, "multi_edit", r"edit 2 of 2.*nothing was changed", path="a.txt", edits=[
        {"old_string": "one", "new_string": "1"}, {"old_string": "missing", "new_string": "x"}])
    assert (ws / "a.txt").read_text() == "one two three\n"


# ---- apply_patch -----------------------------------------------------------------
PATCH = """--- a/app.py
+++ b/app.py
@@ -1,4 +1,4 @@
 def run():
-    return 1
+    return 2

 print(run())
"""


def test_apply_patch_modifies_a_file(ws):
    put(ws, "app.py", "def run():\n    return 1\n\nprint(run())\n")
    assert "1 file" in call(ws, "apply_patch", patch=PATCH)
    assert (ws / "app.py").read_text() == "def run():\n    return 2\n\nprint(run())\n"


def test_apply_patch_finds_a_hunk_whose_line_numbers_are_off(ws):
    put(ws, "app.py", "# header\n# more\n# lines\ndef run():\n    return 1\n\nprint(run())\n")
    call(ws, "apply_patch", patch=PATCH)
    assert "return 2" in (ws / "app.py").read_text()


def test_apply_patch_creates_and_deletes_files(ws):
    put(ws, "gone.txt", "bye\n")
    patch = ("--- /dev/null\n+++ b/made.txt\n@@ -0,0 +1,2 @@\n+line one\n+line two\n"
             "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
    call(ws, "apply_patch", patch=patch)
    assert (ws / "made.txt").read_text() == "line one\nline two\n" and not (ws / "gone.txt").exists()


def test_apply_patch_renames(ws):
    put(ws, "old.txt", "keep\nchange\n")
    patch = "--- a/old.txt\n+++ b/new.txt\n@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n"
    call(ws, "apply_patch", patch=patch)
    assert (ws / "new.txt").read_text() == "keep\nchanged\n" and not (ws / "old.txt").exists()


def test_apply_patch_is_all_or_nothing_across_files(ws):
    put(ws, "a.txt", "a1\n")
    put(ws, "b.txt", "b1\n")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a1\n+A1\n"
             "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-NOT THERE\n+B1\n")
    fails(ws, "apply_patch", "does not apply", patch=patch)
    assert (ws / "a.txt").read_text() == "a1\n" and (ws / "b.txt").read_text() == "b1\n"


def test_apply_patch_rejects_garbage_and_outside_paths(ws):
    fails(ws, "apply_patch", "no file changes", patch="just some words")
    fails(ws, "apply_patch", "outside the working directory",
          patch="--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+x\n")


# ---- grep ------------------------------------------------------------------------
def _tree(ws):
    put(ws, "src/a.py", "import os\nfoo = 1\nBAR = 2\n", read=False)
    put(ws, "src/b.txt", "foo again\n", read=False)
    put(ws, "node_modules/lib/x.js", "foo hidden\n", read=False)
    (ws / "img.bin").write_bytes(b"\x00\x01 foo binary")


def test_grep_content_has_paths_and_line_numbers_and_skips_vendored_and_binary(ws):
    _tree(ws)
    out = call(ws, "grep", pattern="foo")
    assert "src/a.py:2:foo = 1" in out and "src/b.txt:1:foo again" in out
    assert "node_modules" not in out and "img.bin" not in out


def test_grep_modes_glob_and_case(ws):
    _tree(ws)
    assert call(ws, "grep", pattern="foo", output_mode="files").splitlines() == ["src/a.py", "src/b.txt"]
    assert "src/a.py:1" in call(ws, "grep", pattern="foo", output_mode="count")
    assert call(ws, "grep", pattern="foo", glob="*.py", output_mode="files") == "src/a.py"
    assert call(ws, "grep", pattern="bar") == "No matches."
    assert "BAR = 2" in call(ws, "grep", pattern="bar", ignore_case=True)


def test_grep_context_lines_and_fixed_strings(ws):
    put(ws, "c.txt", "l1\nl2\nTARGET (x)\nl4\nl5\n", read=False)
    out = call(ws, "grep", pattern="TARGET", context=1)
    assert "c.txt-2-l2" in out and "c.txt:3:TARGET (x)" in out and "c.txt-4-l4" in out
    assert "TARGET (x)" in call(ws, "grep", pattern="(x)", fixed_strings=True)


def test_grep_errors_and_limits(ws):
    _tree(ws)
    fails(ws, "grep", "invalid regular expression", pattern="(")
    fails(ws, "grep", "outside the working directory", pattern="x", path="..")
    fails(ws, "grep", "does not exist", pattern="x", path="nope")
    for i in range(10):
        put(ws, f"many/{i}.txt", "hit\n", read=False)
    assert "stopped at 3 matches" in call(ws, "grep", pattern="hit", max_results=3)


def test_grep_can_search_saved_tool_output_when_pointed_at_it(ws):
    saved = toolspec._spill(ws, "run_shell", "needle in the haystack\n" * 3)
    assert "needle" in call(ws, "grep", pattern="needle", path=saved)
    assert toolspec.SPILL_DIR not in call(ws, "grep", pattern="needle")


# ---- glob ------------------------------------------------------------------------
def test_glob_finds_files_newest_first_and_skips_vendored_folders(ws):
    import os, time

    a = put(ws, "src/old.py", "1", read=False)
    b = put(ws, "src/new.py", "2", read=False)
    put(ws, "node_modules/x.py", "3", read=False)
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert call(ws, "glob", pattern="**/*.py").splitlines() == ["src/new.py", "src/old.py"]
    assert call(ws, "glob", pattern="*.rs") == "No files match."


def test_glob_rejects_escaping_patterns(ws):
    fails(ws, "glob", "relative", pattern="../*")
    fails(ws, "glob", "relative", pattern="/etc/*")


# ---- todo ------------------------------------------------------------------------
def test_todo_list_round_trips_and_is_per_session(ws):
    assert call(ws, "todo_read") == "No todo list yet."
    out = call(ws, "todo_write", todos=[{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"},
                                        {"content": "c"}])
    assert "1/3 done" in out and "[x] a" in out and "[~] b" in out and "[ ] c" in out
    assert "[~] b" in call(ws, "todo_read")
    other = toolspec.session_var.set("elsewhere")
    try:
        assert call(ws, "todo_read") == "No todo list yet."
    finally:
        toolspec.session_var.reset(other)


def test_todo_validation_and_the_one_in_progress_hint(ws):
    fails(ws, "todo_write", "status must be", todos=[{"content": "a", "status": "doing"}])
    fails(ws, "todo_write", "non-empty content", todos=[{"content": " "}])
    out = call(ws, "todo_write", todos=[{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}])
    assert "one at a time" in out


# ---- read_file -------------------------------------------------------------------
def test_read_file_plain_is_unchanged_and_slices_are_numbered(ws):
    body = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    put(ws, "long.txt", body, read=False)
    assert call(ws, "read_file", path="long.txt") == body
    out = call(ws, "read_file", path="long.txt", offset=3, limit=2)
    assert out.splitlines()[0].strip().startswith("3") and "line 3" in out and "line 4" in out and "line 5" not in out
    assert "offset=5" in out
    assert "no lines at offset 99" in call(ws, "read_file", path="long.txt", offset=99)


def test_read_file_truncates_huge_files_and_says_how_to_continue(ws):
    put(ws, "big.txt", "x" * 50_000, read=False)
    out = call(ws, "read_file", path="big.txt")
    assert "truncated" in out and "offset and limit" in out


def test_read_file_notebooks_binaries_images_and_pdfs(ws):
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Title"]},
                    {"cell_type": "code", "source": ["print(1)"], "outputs": [{"text": ["1\n"]}]}]}
    put(ws, "n.ipynb", json.dumps(nb), read=False)
    out = call(ws, "read_file", path="n.ipynb")
    assert "[cell 0: markdown]" in out and "print(1)" in out and "# [output]" in out
    (ws / "d.bin").write_bytes(b"\x00\x01\x02")
    assert "binary file" in call(ws, "read_file", path="d.bin")
    (ws / "p.png").write_bytes(b"\x89PNG....")
    assert "image file" in call(ws, "read_file", path="p.png")
    (ws / "x.pdf").write_bytes(b"%PDF-1.4 not really")
    assert "PDF" in call(ws, "read_file", path="x.pdf")


# ---- write_file follows the read-first rule ----------------------------------------
def test_write_file_over_an_existing_file_needs_a_read_but_a_new_file_does_not(ws):
    put(ws, "e.txt", "old", read=False)
    fails(ws, "write_file", "read e.txt", path="e.txt", content="new")
    call(ws, "write_file", path="fresh.txt", content="new")
    call(ws, "read_file", path="e.txt")
    call(ws, "write_file", path="e.txt", content="new")
    assert (ws / "e.txt").read_text() == "new"
    call(ws, "edit_file", path="e.txt", old_string="new", new_string="newer")   # no re-read needed after our own write
    assert (ws / "e.txt").read_text() == "newer"


# ---- output spill ----------------------------------------------------------------
def test_large_output_is_saved_in_full_and_the_note_says_where(ws):
    text = "".join(f"row {i}\n" for i in range(20_000))
    out = toolspec.limit_output("some_tool", text, ws)
    assert len(out) < len(text) and "saved at .abp-tool-output/" in out
    saved = next((ws / toolspec.SPILL_DIR).glob("some_tool-*.txt"))
    assert saved.read_text() == text
    assert (ws / toolspec.SPILL_DIR / ".gitignore").read_text().strip() == "*"
    assert "saved at" not in toolspec.limit_output("some_tool", text)   # no workspace, no spill


def test_leaf_subagents_get_the_registered_tools_but_not_the_ones_that_delegate():
    from bot.agent_runtime import subagents

    registered = toolspec.registered_names()
    assert {"edit_file", "grep", "glob", "shell_output"} <= registered
    leaf = (frozenset(tools.TOOL_SCHEMA_NAMES) | registered) - subagents.LEAF_BLOCKED_TOOLS
    assert "edit_file" in leaf and "spawn_subagent" not in leaf
