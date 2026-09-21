"""Formatters and language servers after an edit (roadmap P5), against a stand-in language server."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from bot.agent_runtime import code_intel, coding_tools, tools

FAKE_SERVER = str(Path(__file__).with_name("fake_lsp_server.py"))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(code_intel, "_cfg", lambda: values)
    code_intel._clients.clear()
    code_intel._failed.clear()
    yield values
    run(code_intel.shutdown_all())
    code_intel._clients.clear()
    code_intel._failed.clear()


def with_lsp(cfg, **extra):
    cfg["lsp"] = {"enabled": True, "wait_s": 10, "servers": {"fake": {"command": [sys.executable, FAKE_SERVER], "extensions": [".py"]}}, **extra}


# ---- uris ------------------------------------------------------------------------------------
def test_uris_round_trip(tmp_path):
    p = tmp_path / "a folder" / "x y.py"
    p.parent.mkdir()
    p.write_text("1")
    assert code_intel.uri_of(p).startswith("file:///") and " " not in code_intel.uri_of(p)
    assert code_intel.path_of(code_intel.uri_of(p)).resolve() == p.resolve()


# ---- language server after an edit ---------------------------------------------------------------
def edit(workspace, path, old, new):
    return run(tools.execute_tool("edit_file", {"path": path, "old_string": old, "new_string": new}, workspace=workspace, instance_id=1))


def test_an_edit_reports_the_problems_the_server_finds(tmp_path, cfg):
    with_lsp(cfg)
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    out = edit(tmp_path, "a.py", "y = 2", "y = BROKEN  # TODO later")
    assert "Edited a.py" in out and "Problems the language server found after this edit" in out
    assert "a.py:2:5 error: found BROKEN [fake]" in out and "a.py:2:" in out and "warning" not in out    # warnings are off by default


def test_servers_that_answer_on_request_instead_of_pushing_are_asked(tmp_path, cfg, monkeypatch):
    """rust-analyzer and recent pyright advertise diagnosticProvider and never push (seen with real rust-analyzer)."""
    monkeypatch.setenv("FAKE_LSP_PULL", "1")
    with_lsp(cfg, wait_s=3)
    (tmp_path / "a.py").write_text("x = 1\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    out = edit(tmp_path, "a.py", "x = 1", "x = BROKEN")
    assert "a.py:1:5 error: found BROKEN [fake]" in out
    assert "Problems" not in edit(tmp_path, "a.py", "x = BROKEN", "x = 2")


def test_a_server_still_loading_is_asked_again(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_PULL", "1")
    monkeypatch.setenv("FAKE_LSP_LOADING", "3")
    with_lsp(cfg, wait_s=5)
    (tmp_path / "a.py").write_text("x = 1\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    assert "a.py:1:5 error: found BROKEN" in edit(tmp_path, "a.py", "x = 1", "x = BROKEN")


def test_a_clean_edit_adds_nothing(tmp_path, cfg):
    with_lsp(cfg)
    (tmp_path / "a.py").write_text("x = 1\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    assert "Problems" not in edit(tmp_path, "a.py", "x = 1", "x = 2")


def test_warnings_can_be_included_and_capped(tmp_path, cfg):
    with_lsp(cfg, include_warnings=True, max_problems=2)
    (tmp_path / "a.py").write_text("ok\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    out = edit(tmp_path, "a.py", "ok", "TODO 1\nTODO 2\nTODO 3\nBROKEN")
    assert "error: found BROKEN" in out and "warning: found TODO" in out and "and 2 more" in out


def test_the_second_edit_reuses_the_running_server_and_sees_the_new_text(tmp_path, cfg):
    with_lsp(cfg)
    (tmp_path / "a.py").write_text("BROKEN\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    assert "found BROKEN" in edit(tmp_path, "a.py", "BROKEN", "BROKEN again")
    assert "Problems" not in edit(tmp_path, "a.py", "BROKEN again", "fixed")
    assert len(code_intel._clients) == 1


def test_a_write_file_and_a_patch_are_followed_up_too(tmp_path, cfg):
    with_lsp(cfg)
    out = run(tools.execute_tool("write_file", {"path": "b.py", "content": "BROKEN\n"}, workspace=tmp_path, instance_id=1))
    assert "Wrote" in out and "b.py:1:1 error" in out
    (tmp_path / "c.py").write_text("ok\n")
    run(tools.execute_tool("read_file", {"path": "c.py"}, workspace=tmp_path, instance_id=1))
    patch = "--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-ok\n+BROKEN\n"
    assert "c.py:1:1 error" in run(tools.execute_tool("apply_patch", {"patch": patch}, workspace=tmp_path, instance_id=1))


def test_a_file_type_without_a_server_and_a_disabled_setting_change_nothing(tmp_path, cfg):
    with_lsp(cfg)
    (tmp_path / "n.txt").write_text("BROKEN\n")
    run(tools.execute_tool("read_file", {"path": "n.txt"}, workspace=tmp_path, instance_id=1))
    assert "Problems" not in edit(tmp_path, "n.txt", "BROKEN", "BROKEN 2")
    cfg["lsp"]["enabled"] = False
    (tmp_path / "a.py").write_text("x\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    assert "Problems" not in edit(tmp_path, "a.py", "x", "BROKEN")


def test_a_missing_or_crashing_server_never_breaks_the_edit(tmp_path, cfg):
    cfg["lsp"] = {"enabled": True, "servers": {"gone": {"command": ["definitely-not-installed-xyz"], "extensions": [".py"]}}}
    (tmp_path / "a.py").write_text("x = 1\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    out = edit(tmp_path, "a.py", "x = 1", "x = 2")
    assert out.startswith("Edited a.py") and (tmp_path / "a.py").read_text() == "x = 2\n"
    assert code_intel._failed, "the failure is remembered so every edit does not pay for a start attempt"
    cfg["lsp"] = {"enabled": True, "wait_s": 1, "servers": {"bad": {"command": [sys.executable, "-c", "import sys; sys.exit(3)"], "extensions": [".py"]}}}
    code_intel._failed.clear()
    assert edit(tmp_path, "a.py", "x = 2", "x = 3").startswith("Edited a.py")


def test_a_server_that_stays_silent_is_reported_not_waited_on_forever(tmp_path, cfg):
    silent = "import sys\nwhile sys.stdin.buffer.readline():\n    pass\n"
    (tmp_path / "silent.py").write_text(silent)
    cfg["lsp"] = {"enabled": True, "wait_s": 1, "start_timeout_s": 1,
                  "servers": {"quiet": {"command": [sys.executable, str(tmp_path / "silent.py")], "extensions": [".py"]}}}
    (tmp_path / "a.py").write_text("x = 1\n")
    run(tools.execute_tool("read_file", {"path": "a.py"}, workspace=tmp_path, instance_id=1))
    assert edit(tmp_path, "a.py", "x = 1", "x = 2").startswith("Edited a.py")          # it never answered initialize: skipped quietly


# ---- the lsp tool -----------------------------------------------------------------------------------------
def lsp(workspace, **inp):
    return run(tools.execute_tool("lsp", inp, workspace=workspace, instance_id=1))


def test_the_lsp_tool_answers_questions(tmp_path, cfg):
    with_lsp(cfg)
    (tmp_path / "a.py").write_text("class Outer:\n    def inner(self):\n        pass\n\nTODO\nBROKEN\n")
    assert "6:1 error: found BROKEN" in lsp(tmp_path, action="diagnostics", path="a.py") and "warning: found TODO" in lsp(tmp_path, action="diagnostics", path="a.py")
    assert lsp(tmp_path, action="symbols", path="a.py") == "Outer (kind 5) line 1\n  inner (kind 6) line 2"
    assert lsp(tmp_path, action="definition", path="a.py", line=1, column=1) == "a.py:4:5"
    assert lsp(tmp_path, action="references", path="a.py", line=1, column=1).splitlines() == ["a.py:1:1", "a.py:8:3"]
    assert lsp(tmp_path, action="hover", path="a.py", line=1, column=1) == "def add(a, b) -> int"


def test_the_lsp_tool_refuses_clearly(tmp_path, cfg):
    (tmp_path / "a.py").write_text("x\n")
    with pytest.raises(Exception, match="unknown tool"):                     # not even offered while disabled
        lsp(tmp_path, action="symbols", path="a.py")
    with_lsp(cfg)
    with pytest.raises(Exception, match="action must be"):
        lsp(tmp_path, action="rename", path="a.py")
    with pytest.raises(Exception, match="not a file"):
        lsp(tmp_path, action="symbols", path="missing.py")
    with pytest.raises(Exception, match="outside|escape|inside"):
        lsp(tmp_path, action="symbols", path="../a.py")
    assert not tools.is_dangerous("lsp")


def test_the_lsp_tool_is_only_offered_when_enabled(cfg):
    assert "lsp" not in {s["name"] for s in tools.all_tool_schemas()}
    with_lsp(cfg)
    assert "lsp" in {s["name"] for s in tools.all_tool_schemas()}


# ---- formatters --------------------------------------------------------------------------------------------------
FORMATTER = ("import sys\np = sys.argv[1]\ns = open(p).read()\nopen(p, 'w').write(' '.join(s.split()) + '\\n')\n")


def test_a_configured_formatter_runs_after_an_edit_and_the_next_edit_is_not_stale(tmp_path, cfg):
    (tmp_path / "fmt.py").write_text(FORMATTER)
    cfg["formatters"] = {".txt": [sys.executable, str(tmp_path / "fmt.py"), "{path}"]}
    (tmp_path / "a.txt").write_text("a  b\n")
    run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=1))
    out = edit(tmp_path, "a.txt", "a  b", "c  d   e")
    assert "(formatted a.txt with" in out and (tmp_path / "a.txt").read_text() == "c d e\n"
    assert edit(tmp_path, "a.txt", "c d e", "done").startswith("Edited a.txt")           # no "changed since you read it" refusal


def test_formatter_problems_are_reported_and_leave_the_file_as_written(tmp_path, cfg):
    (tmp_path / "a.txt").write_text("x\n")
    run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=1))
    cfg["formatters"] = {".txt": ["definitely-not-installed-xyz", "{path}"]}
    assert "is not installed" in edit(tmp_path, "a.txt", "x", "y")
    cfg["formatters"] = {".txt": [sys.executable, "-c", "import sys; sys.stderr.write('bad syntax'); sys.exit(1)"]}
    assert "failed on a.txt: bad syntax" in edit(tmp_path, "a.txt", "y", "z") and (tmp_path / "a.txt").read_text() == "z\n"


def test_a_formatter_command_can_be_a_string_and_uses_no_shell(tmp_path, cfg):
    marker = tmp_path / "marker"
    cfg["formatters"] = {".txt": f'{sys.executable} -c "open(r\'{marker}\', \'w\').write(\'ran\')" {{path}}; echo pwned > {tmp_path / "pwned"}'}
    (tmp_path / "a.txt").write_text("x\n")
    run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=1))
    edit(tmp_path, "a.txt", "x", "y")
    assert not (tmp_path / "pwned").exists()


def test_formatter_environment_has_no_secrets(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("MY_SECRET_API_KEY", "should-not-leak")
    (tmp_path / "fmt.py").write_text("import os,sys\nopen(sys.argv[1],'a').write(os.environ.get('MY_SECRET_API_KEY','absent'))\n")
    cfg["formatters"] = {".txt": [sys.executable, str(tmp_path / "fmt.py"), "{path}"]}
    (tmp_path / "a.txt").write_text("x\n")
    run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=1))
    edit(tmp_path, "a.txt", "x", "y")
    assert (tmp_path / "a.txt").read_text().endswith("absent")
