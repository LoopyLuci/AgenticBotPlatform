"""TokenJuice (bot/agent_runtime/tokenjuice.py): tool output compacted by kind, never grown, the original kept and
readable through the tool_output tool."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from bot.agent_runtime import tokenjuice as tj


def _handle(out: str) -> str:
    return re.search(r"⟦tj:([0-9a-f]+)⟧", out).group(1)


def test_small_output_passes_through_and_nothing_grows():
    assert tj.compress("short output", "run_shell") == "short output"
    dense = "".join(chr(0x4E00 + i % 500) for i in range(3000))           # nothing to cut in plain prose-ish text
    assert len(tj.compress(dense, "read_file")) <= len(dense)


def test_detection():
    assert tj.detect(json.dumps([{"a": 1}] * 50)) == "json"
    assert tj.detect("diff --git a/x b/x\n@@ -1,2 +1,2 @@\n-a\n+b\n") == "diff"
    assert tj.detect("<html><body><div>hi</div></body></html>") == "html"
    assert tj.detect("\n".join(f"src/m{i}.py:{i}:    x = {i}" for i in range(20))) == "search"
    assert tj.detect("import os\ndef a():\n  pass\nclass B:\n  pass\ndef c():\n  pass\n") == "code"
    assert tj.detect("ERROR boom\nWARNING x\nerror again\n") == "log"
    assert tj.detect("plain words here") == "text"


def test_json_arrays_become_tables_and_keep_error_rows():
    rows = [{"id": i, "status": "ok", "ms": 10 + i % 3} for i in range(300)]
    rows[150] = {"id": 150, "status": "error: disk full", "ms": 12}
    out = tj.compress(json.dumps(rows), "http_get")
    assert out.splitlines()[0] == "id | status | ms" and "error: disk full" in out and "more items" in out
    h = _handle(out)
    assert json.loads(tj.read(h, 1, 1_000_000).rsplit("\n[", 1)[0])[299]["id"] == 299    # the original, whole


def test_diffs_logs_search_code_and_html():
    body = "\n".join(f" unchanged {i}" for i in range(200))
    diff = f"diff --git a/app.py b/app.py\n@@ -1,203 +1,203 @@\n{body}\n-old line\n+new line\n" \
           "diff --git a/package-lock.json b/package-lock.json\n" + "\n".join(f"+dep{i}" for i in range(400))
    out = tj.compress(diff, "git_diff")
    assert "-old line" in out and "+new line" in out and "unchanged lines" in out and "(lockfile: +400/-0 lines)" in out
    log = "\n".join([f"Compiling crate{i} v0.1.{i}" for i in range(400)] + ["error[E0308]: mismatched types",
                                                                           "  --> src/main.rs:4:5"] + ["done"] * 3)
    out = tj.compress(log, "run_shell")
    assert "error[E0308]: mismatched types" in out and len(out) < len(log) / 3
    assert "repeat" in out or out.count("Compiling") < 60
    hits = "\n".join(f"src/file{i % 4}.py:{i}: value = compute(alpha, {i})" for i in range(300))
    out = tj.compress(hits, "grep")
    assert out.count("src/file0.py:") == 1 and "[+67 more]" in out
    code = "import os\n\n" + "\n".join(f"def f{i}(x):\n" + "\n".join(f"    y = x + {j}" for j in range(30)) + "\n    return y"
                                       for i in range(20)) + "\n# TODO: speed up f3\n"
    out = tj.compress(code, "read_file")
    assert "def f7(x):" in out and "lines ... }" in out and "TODO: speed up f3" in out and "y = x + 17" not in out
    html = "<html><head><script>var x=1;</script></head><body>" + "<p>Hello &amp; welcome</p>" * 200 + "</body></html>"
    out = tj.compress(html, "web_fetch")
    assert "Hello & welcome" in out and "<p>" not in out and "var x" not in out


def test_reading_back_and_stats(tmp_path):
    from bot.agent_runtime import tools
    log = "\n".join(f"line {i} " + ("ERROR bad thing" if i == 777 else "fine") for i in range(2000))
    out = tj.compress(log, "run_shell")
    h = _handle(out)
    got = asyncio.run(tools.execute_tool("tool_output", {"handle": h, "pattern": "ERROR"}, workspace=tmp_path))
    assert got == "778: line 777 ERROR bad thing"
    page = asyncio.run(tools.execute_tool("tool_output", {"handle": h, "start": 1999, "lines": 5}, workspace=tmp_path))
    assert page.startswith("line 1998 fine\nline 1999 fine") and "[end; total 2000 lines]" in page
    with pytest.raises(tools.ToolError, match="no kept output"):
        asyncio.run(tools.execute_tool("tool_output", {"handle": "0" * 12}, workspace=tmp_path))
    s = tj.stats()
    assert s["compressed"] >= 1 and s["chars_saved"] > 0 and s["by_kind"]["log"]["chars_saved"] > 0


def test_the_tool_loop_compacts_and_the_switch_turns_it_off(monkeypatch):
    from bot.agent_runtime import toolspec
    big = "\n".join(f"Compiling crate{i}" for i in range(2000)) + "\nerror: failed\n"
    assert "error: failed" in toolspec.limit_output("run_shell", big) and len(toolspec.limit_output("run_shell", big)) < 12_500
    monkeypatch.setenv("ABP_TOKENJUICE", "0")
    assert tj.compress(big, "run_shell") == big
