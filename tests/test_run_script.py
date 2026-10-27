"""run_script (bot/agent_runtime/run_script.py): the repository's own generator scripts, allowlisted, and only inside a
confining sandbox without network. Also the edit tools' read-back check (coding_tools.verify_saved)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.agent_runtime import coding_tools, run_script, sandbox, shell
from bot.agent_runtime.errors import ToolError


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "export_openapi.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "scripts" / "other.py").write_text("print('x')\n", encoding="utf-8")
    return tmp_path


def confined(monkeypatch, backend="windows_job", net="none"):
    monkeypatch.setattr(sandbox, "backend", lambda: backend)
    monkeypatch.setattr(sandbox, "network", lambda cfg=None: net)


def test_only_allowlisted_scripts_without_arguments(ws, monkeypatch):
    confined(monkeypatch)
    for bad in ("scripts/other.py", "scripts/export_openapi.py --out x", "../x.py", "", None, "rm -rf ."):
        with pytest.raises(ToolError):
            run_script.clean_script(ws, bad)
    assert run_script.clean_script(ws, "./scripts/export_openapi.py") == "scripts/export_openapi.py"
    (ws / "scripts" / "export_openapi.py").unlink()
    with pytest.raises(ToolError, match="does not exist"):
        run_script.clean_script(ws, "scripts/export_openapi.py")


@pytest.mark.parametrize("backend,net", [("local", "none"), ("windows_job", "allow"), ("docker", "allowlist")])
def test_it_refuses_without_a_confining_sandbox_and_no_network(ws, monkeypatch, backend, net):
    confined(monkeypatch, backend, net)
    ran = []

    async def fake(*a):
        ran.append(a)
        return ""

    monkeypatch.setattr(shell, "_run_fresh", fake)
    with pytest.raises(ToolError, match="run_script needs"):
        asyncio.run(run_script._run_script({"script": "scripts/export_openapi.py"}, workspace=ws))
    assert ran == []


def test_it_runs_the_script_through_the_sandbox(ws, monkeypatch):
    confined(monkeypatch)
    seen = {}

    async def fake(command, where, workspace, limit):
        seen.update(command=command, where=where, limit=limit)
        return "wrote 4 files\n[exit code 0]"

    monkeypatch.setattr(shell, "_run_fresh", fake)
    out = asyncio.run(run_script._run_script({"script": "scripts/export_openapi.py"}, workspace=ws))
    assert out.startswith("ran scripts/export_openapi.py") and "wrote 4 files" in out
    assert seen["command"].endswith("scripts/export_openapi.py") and seen["limit"] == run_script.DEFAULT_TIMEOUT_S
    assert Path(seen["where"]) == ws.resolve()


def test_the_tool_is_registered():
    from bot.agent_runtime import tools

    assert "run_script" in {s["name"] for s in tools.all_tool_schemas()}


def test_an_edit_that_breaks_python_says_so_and_a_lost_write_is_an_error(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert coding_tools.verify_saved(p, "m.py", p.read_text(encoding="utf-8")) == ""
    p.write_text("def f(:\n", encoding="utf-8")
    assert "no longer parses" in coding_tools.verify_saved(p, "m.py", "def f(:\n")
    with pytest.raises(ToolError, match="does not hold the edit"):
        coding_tools.verify_saved(p, "m.py", "something else\n")
