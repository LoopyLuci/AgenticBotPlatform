"""Plugin SDK versioning and the benchmark page (roadmap P9)."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from abp_agenteval import page
from bot import plugins


@pytest.mark.parametrize("requirement,expected", [
    (">=1.0,<2", True), (">=1", True), ("==1.0", True), ("==1.0.0", True), (">=1.1", False), (">=2", False), ("<1", False), ("<=1.0", True),
    ("!=1.0", False), (">0.9,<1.5", True), (" >= 1.0 , < 2 ", True)])
def test_sdk_requirements_are_read_and_compared(requirement, expected):
    assert plugins.sdk_satisfies(requirement) is expected


def test_the_comparison_uses_numbers_not_strings():
    assert plugins.sdk_satisfies(">=1.2", "1.10") is True and plugins.sdk_satisfies("<1.10", "1.9") is True


@pytest.mark.parametrize("bad", ["", "1.0", "~1.0", ">=one", ">=1.0;<2", ">=1.0,,<2"])
def test_a_requirement_that_cannot_be_read_is_an_error(bad):
    with pytest.raises(ValueError):
        plugins.sdk_satisfies(bad)


def plugin(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return path


TOOL = "def setup(api):\n    async def h(inp, *, workspace=None, instance_id=None):\n        return 'ok'\n    api.register_tool('{name}_tool', 'd', {{'type': 'object', 'properties': {{}}}}, h)\n"


def test_a_plugin_for_this_sdk_installs_and_shows_its_versions(tmp_path, temp_db):
    p = plugin(tmp_path, "good", 'REQUIRES_SDK = ">=1.0,<2"\nPLUGIN_VERSION = "2.3.1"\nPLUGIN_DESCRIPTION = "d"\n' + TOOL.format(name="good"))
    info = plugins.install(str(p))
    assert info["version"] == "2.3.1" and info["requires_sdk"] == ">=1.0,<2" and info["sdk_version"] == plugins.SDK_VERSION and info["tools"] == ["good_tool"]


def test_a_plugin_for_a_different_sdk_is_refused_with_both_versions_named(tmp_path, temp_db):
    p = plugin(tmp_path, "future", 'REQUIRES_SDK = ">=2.0"\n' + TOOL.format(name="future"))
    with pytest.raises(plugins.PluginError, match=r"needs plugin SDK >=2.0, but this build provides " + plugins.SDK_VERSION.replace(".", r"\.")):
        plugins.install(str(p))
    assert not plugins.has_tool("future_tool"), "nothing is left registered"


def test_a_plugin_with_an_unreadable_requirement_is_refused(tmp_path, temp_db):
    p = plugin(tmp_path, "garbled", 'REQUIRES_SDK = "one point oh"\n' + TOOL.format(name="garbled"))
    with pytest.raises(plugins.PluginError, match="cannot read"):
        plugins.install(str(p))


def test_a_plugin_that_says_nothing_still_installs(tmp_path, temp_db):
    p = plugin(tmp_path, "plain", TOOL.format(name="plain"))
    info = plugins.install(str(p))
    assert info["requires_sdk"] == "" and info["version"] == "" and info["tools"] == ["plain_tool"]


# ---- the benchmark page ------------------------------------------------------------------------------------------------------
def report(model="scripted", mode="scripted", passed=2, results=None):
    results = results if results is not None else [
        {"id": "a", "title": "Task A", "category": "files", "passed": True, "checks": []},
        {"id": "b", "title": "Task <B>", "category": "coding", "passed": passed == 2, "checks": [{"name": "file_equals", "ok": False, "detail": "expected <x>"}]}]
    return {"mode": mode, "model": model, "when": 1_800_000_000, "total": len(results), "passed": sum(1 for r in results if r["passed"]),
            "score": 100.0 * sum(1 for r in results if r["passed"]) / len(results), "tokens": 10, "results": results}


class Strict(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ("meta", "br", "col", "input", "link"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


def test_the_page_says_a_scripted_run_is_not_a_model_comparison():
    text = page.render_html([report()])
    assert "This is not a model comparison" in text and "No live run is included" in text and "were not run" in text
    p = Strict()
    p.feed(text)
    assert not p.errors and not p.stack


def test_one_live_model_is_still_not_a_comparison_and_two_are():
    one = page.render_html([report(), report("openrouter/x", "live")])
    assert "Only one model (openrouter/x) has a live run" in one
    two = page.render_html([report("openrouter/x", "live"), report("anthropic/y", "live")])
    assert "not a model comparison" not in two


def test_failures_are_listed_with_their_reason_and_everything_is_escaped():
    text = page.render_html([report(passed=1)])
    assert "Failures" in text and "expected &lt;x&gt;" in text and "Task &lt;B&gt;" in text and "<B>" not in text


def test_a_task_missing_from_one_report_shows_a_dash():
    a = report()
    b = report("m", "live", results=[{"id": "a", "title": "Task A", "category": "files", "passed": True, "checks": []}])
    assert "<td class=muted>-</td>" in page.render_html([a, b])


def test_the_command_writes_the_file_and_rejects_bad_input(tmp_path, capsys):
    from abp_agenteval.__main__ import main

    src = tmp_path / "r.json"
    src.write_text(json.dumps(report()))
    out = tmp_path / "site" / "index.html"
    assert main(["page", str(src), "--out", str(out)]) == 0 and "Task A" in out.read_text(encoding="utf-8")
    assert main(["page", str(tmp_path / "missing.json"), "--out", str(out)]) == 2


def test_the_committed_benchmark_page_is_present_and_honest():
    root = Path(__file__).resolve().parent.parent / "docs" / "benchmarks"
    text = (root / "index.html").read_text(encoding="utf-8")
    assert "This is not a model comparison" in text and (root / "scripted-report.json").exists()
