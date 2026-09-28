"""abp_toolkit: every action registered and described, and each group doing its job on real files (nothing mocked:
the linters, formatters, runners and image code all run for real; programs that may be missing are skipped)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from abp_toolkit import registry
from abp_toolkit.registry import ToolkitError, call, catalog

WIN = os.name == "nt"


def test_every_action_is_described_and_grouped():
    acts = catalog()
    assert len(acts) >= 60
    groups = {a["group"] for a in acts}
    assert groups == set(registry.GROUPS) == {"lint", "format", "run", "analyze", "python", "cs", "generate", "asset", "scripts"}
    for a in acts:
        assert a["summary"], a["id"]
        assert a["params"]["type"] == "object"
        assert "workspace" not in a["params"]["properties"], a["id"]


def test_paths_cannot_leave_the_workspace(tmp_path):
    with pytest.raises(ToolkitError, match="outside the working folder"):
        call("lint.check", {"path": ".."}, workspace=tmp_path)
    with pytest.raises(ToolkitError, match="outside"):
        call("asset.icon", {"path": "../evil.png", "text": "x"}, workspace=tmp_path)


def test_unknown_arguments_are_refused(tmp_path):
    with pytest.raises(ToolkitError, match="unknown argument"):
        call("cs.number", {"value": "1", "colour": 2}, workspace=tmp_path)


# ---- lint -----------------------------------------------------------------------------------------------------------
SAMPLES = {
    "bad.json": ('{"a": 1,}', "syntax"),
    "bad.yaml": ("key: [1, 2\n", "syntax"),
    "bad.toml": ("a = [1,\n", "syntax"),
    "bad.xml": ("<a><b></a>", "syntax"),
    "bad.bat": ("@echo off\r\ngoto nowhere\r\n", "missing-label"),
    "bad.vbs": ("Option Explicit\r\nSub A()\r\n  If x Then\r\nEnd Sub\r\n", "unclosed-block"),
    "bad.html": ("<div><span>hi</div>", "unclosed-tag"),
    "bad.css": ("a { color: red;\n", "unbalanced"),
    "bad.sql": ("DELETE FROM users;\n", "no-where"),
    "Dockerfile": ("FROM python\nRUN apt-get install curl\n", "DL3006"),
    "leak.txt": ("aws = AKIA" + "ABCDEFGHIJKLMNOP\n", "secret/aws-access-key"),
}


def test_lint_finds_problems_in_many_languages(tmp_path):
    for name, (text, _code) in SAMPLES.items():
        (tmp_path / name).write_bytes(text.encode())
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n")
    r = call("lint.check", {"external": False}, workspace=tmp_path)
    codes = {(f["file"], f["code"]) for f in r["findings"]}
    for name, (_t, code) in SAMPLES.items():
        assert (name, code) in codes, (name, code, [c for c in codes if c[0] == name])
    assert ("bad.py", "syntax") in codes
    assert r["counts"]["error"] >= 8


@pytest.mark.skipif(not (shutil.which("pwsh") or shutil.which("powershell")), reason="PowerShell not installed")
def test_lint_parses_powershell_with_its_own_parser(tmp_path):
    (tmp_path / "bad.ps1").write_text("function F { param($a\n}\n")
    r = call("lint.check", {"languages": ["powershell"]}, workspace=tmp_path)
    assert any(f["tool"] == "powershell-parser" and f["severity"] == "error" for f in r["findings"])


def test_lint_snippet_and_clean_code(tmp_path):
    assert call("lint.snippet", {"code": "import os\nprint(os.sep)\n", "language": "python"})["counts"]["error"] == 0
    assert call("lint.snippet", {"code": "{\"a\": }", "language": "json"})["counts"]["error"] == 1


# ---- format -----------------------------------------------------------------------------------------------------------
def test_format_json_and_whitespace(tmp_path):
    assert call("format.snippet", {"code": '{"b":1,"a":[1,2]}', "language": "json"})["code"] == '{\n  "b": 1,\n  "a": [\n    1,\n    2\n  ]\n}\n'
    (tmp_path / "x.txt").write_bytes(b"a  \r\nb\t\r\n\r\n\r\n")
    assert call("format.whitespace", {"path": "x.txt", "check": True}, workspace=tmp_path)["changed"] == ["x.txt"]
    call("format.whitespace", {"path": "x.txt"}, workspace=tmp_path)
    assert (tmp_path / "x.txt").read_bytes() == b"a\r\nb\r\n"       # CRLF files stay CRLF
    call("format.line_endings", {"path": "x.txt", "style": "lf"}, workspace=tmp_path)
    assert (tmp_path / "x.txt").read_bytes() == b"a\nb\n"


# ---- run --------------------------------------------------------------------------------------------------------------
def test_run_python_snippet_with_stdin_and_timeout():
    r = call("run.snippet", {"code": "import sys; print(sys.stdin.read().upper())", "language": "python", "stdin": "hi"})
    assert r["exit_code"] == 0 and r["stdout"].strip() == "HI"
    slow = call("run.snippet", {"code": "import time; time.sleep(5)", "language": "python", "timeout_s": 1})
    assert slow["timed_out"]


@pytest.mark.skipif(not WIN, reason="Windows scripting hosts")
def test_run_batch_and_vbscript():
    assert call("run.snippet", {"code": "echo %1", "language": "batch", "args": ["ok"]})["stdout"].strip() == "ok"
    assert call("run.snippet", {"code": 'WScript.Echo "vbs"', "language": "vbscript"})["stdout"].strip() == "vbs"


# ---- analyze ----------------------------------------------------------------------------------------------------------
def test_analyze_complexity_imports_risks_and_todos(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("")
    (tmp_path / "pkg/a.py").write_text("from pkg import b\n\ndef f(x):\n    if x:\n        for i in x:\n            if i and x or i:\n                return i\n    return None\n")
    (tmp_path / "pkg/b.py").write_text("import subprocess\nfrom pkg import a  # TODO: break this cycle\n\ndef g(c):\n    return subprocess.run(c, shell=True)\n")
    cx = call("analyze.complexity", {"threshold": 1}, workspace=tmp_path)
    assert cx["worst"][0]["name"] == "f" and cx["worst"][0]["complexity"] >= 5
    imp = call("analyze.imports", {}, workspace=tmp_path)
    assert ["pkg.a", "pkg.b"] in imp["cycles"]
    risks = call("analyze.risks", {}, workspace=tmp_path)
    assert any(f["rule"] == "shell-true" for f in risks["findings"])
    assert call("analyze.todos", {}, workspace=tmp_path)["by_tag"] == {"TODO": 1}
    assert {s["name"] for s in call("analyze.symbols", {"path": "pkg/a.py"}, workspace=tmp_path)["symbols"]} == {"f"}


# ---- cs ---------------------------------------------------------------------------------------------------------------
def test_cs_basics():
    assert call("cs.regex", {"pattern": r"(\d+)", "text": "a1 b22", "replace": "<\\1>"})["replaced"] == "a<1> b<22>"
    assert call("cs.encode", {"text": "aGk=", "encoding": "base64", "decode": True})["decoded"] == "hi"
    assert call("cs.number", {"value": "-1"})["u8"]["unsigned"] == 255
    assert call("cs.graph", {"edges": [["a", "b", 1], ["b", "c", 1], ["a", "c", 5]], "algorithm": "shortest_path",
                             "source": "a", "target": "c"})["path"] == ["a", "b", "c"]
    assert call("cs.json_query", {"document": {"a": [{"id": 1}, {"b": {"id": 2}}]}, "path": "$..id"})["values"] == [1, 2]
    assert call("cs.sql", {"query": "select count(*) from t", "json_rows": {"t": [{"x": 1}, {"x": 2}]}})["rows"] == [[2]]
    assert call("cs.cron", {"expression": "0 0 * * 7", "count": 1, "start": "2026-09-28T00:00:00+00:00"})["next"] == ["2026-10-04T00:00:00+00:00"]
    v = call("cs.json_schema", {"document": {"a": 1}, "schema": {"type": "object", "properties": {"a": {"type": "string"}}}})
    assert not v["valid"] and v["errors"][0]["path"] == "/a"


# ---- generate ---------------------------------------------------------------------------------------------------------
def test_generated_python_project_passes_its_own_test(tmp_path):
    call("generate.project", {"template": "python-cli", "name": "Demo Tool"}, workspace=tmp_path)
    root = tmp_path / "demo-tool"
    assert (root / "LICENSE").exists() and (root / ".editorconfig").exists()
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=root, capture_output=True,
                       text=True, env={**os.environ, "PYTHONPATH": str(root / "src")})
    assert r.returncode == 0, r.stdout + r.stderr
    with pytest.raises(ToolkitError, match="already exist"):
        call("generate.project", {"template": "python-cli", "name": "Demo Tool"}, workspace=tmp_path)


# ---- assets -----------------------------------------------------------------------------------------------------------
def test_assets_are_real_files(tmp_path):
    from PIL import Image
    call("asset.icon", {"path": "i.ico", "text": "A"}, workspace=tmp_path)
    assert Image.open(tmp_path / "i.ico").format == "ICO"
    call("asset.icon", {"path": "i.png", "text": "AB", "sizes": [64]}, workspace=tmp_path)
    assert Image.open(tmp_path / "i.png").size == (64, 64)
    call("asset.chart", {"path": "c.svg", "kind": "bar", "labels": ["a", "b"], "series": {"s": [1, 2]}}, workspace=tmp_path)
    call("asset.diagram", {"path": "d.svg", "edges": [["a", "b"], ["b", "c"]]}, workspace=tmp_path)
    call("asset.badge", {"path": "b.svg", "label": "build", "message": "ok"}, workspace=tmp_path)
    for f in ("c.svg", "d.svg", "b.svg"):
        ET.parse(tmp_path / f)                                   # well-formed SVG
    call("asset.qr", {"path": "q.png", "data": "hello"}, workspace=tmp_path)
    call("asset.sound", {"path": "s.wav", "notes": ["A4:0.05", "rest:0.02"]}, workspace=tmp_path)
    import wave
    with wave.open(str(tmp_path / "s.wav")) as w:
        assert w.getnframes() == int(44100 * 0.05) + int(44100 * 0.02)
    call("asset.image", {"path": "o.png", "width": 320, "height": 180, "text": "Hi"}, workspace=tmp_path)
    r = call("asset.convert", {"source": "o.png", "path": "o.webp", "width": 160}, workspace=tmp_path)
    assert r["to"] == [160, 90]
    assert call("asset.contrast", {"foreground": "#000", "background": "#fff"})["ratio"] == 21.0


# ---- scripts ----------------------------------------------------------------------------------------------------------
def test_script_library_headers_and_running(tmp_path):
    lib = call("scripts.list", {})
    assert len(lib) >= 30 and {s["language"] for s in lib} == {"powershell", "cmd", "vbscript", "python", "bash"}
    assert all(s["description"] and s["safety"] in ("read", "changes", "executes", "network") for s in lib)
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    r = call("scripts.run", {"name": "find_duplicates", "args": ["."]}, workspace=tmp_path)
    assert r["exit_code"] == 0 and "dup" in r["stdout"]
    assert (tmp_path / "b.txt").exists()                        # nothing deleted without --delete
    call("scripts.install", {"name": "tree"}, workspace=tmp_path)
    assert (tmp_path / "scripts/tree.py").exists()


@pytest.mark.skipif(not (shutil.which("pwsh") or shutil.which("powershell")), reason="PowerShell not installed")
def test_every_powershell_script_parses(tmp_path):
    lib = [s for s in call("scripts.list", {"language": "powershell"})]
    (tmp_path / "check").mkdir()
    for s in lib:
        src = call("scripts.show", {"name": s["file"]})["source"]
        (tmp_path / "check" / Path(s["file"]).name).write_text(src, encoding="utf-8")
    r = call("lint.check", {"path": "check", "languages": ["powershell"], "min_severity": "error"}, workspace=tmp_path)
    assert r["counts"]["error"] == 0, r["findings"]


# ---- MCP and agent tools ------------------------------------------------------------------------------------------------
def test_mcp_server_offers_groups_and_honours_read_only(tmp_path):
    from abp_toolkit.mcp import Server
    s = Server(tmp_path, read_only=True)
    names = {t["name"] for t in s.tools}
    assert {"toolkit_lint", "toolkit_cs", "toolkit_describe"} <= names

    async def go():
        ok = await s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "toolkit_cs", "arguments": {"action": "number", "args": {"value": "7"}}}})
        blocked = await s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "toolkit_asset", "arguments": {"action": "icon", "args": {"path": "x.png"}}}})
        return ok, blocked
    ok, blocked = asyncio.run(go())
    assert not ok["result"]["isError"] and json.loads(ok["result"]["content"][0]["text"])["decimal"] == 7
    assert blocked["result"]["isError"]


def test_agent_tools_split_looking_from_acting():
    import bot.agent_runtime.toolkit_tools  # noqa: F401
    from bot.agent_runtime import toolspec
    assert not toolspec.registered_dangerous("toolkit_lint") and not toolspec.registered_dangerous("toolkit_analyze")
    for name in ("toolkit_asset_act", "toolkit_run_act", "toolkit_generate_act", "toolkit_scripts_act"):
        assert toolspec.registered_dangerous(name), name
    out = asyncio.run(toolspec.dispatch("toolkit_run", {"action": "snippet", "args": {"code": "print(1)", "language": "python"}}))
    assert "action is one of" in out                            # running code is only on the _act tool
