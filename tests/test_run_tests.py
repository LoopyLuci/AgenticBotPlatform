"""run_tests: pytest on named test paths for agents without a shell."""
from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import run_tests, sandbox, toolspec, tools
from bot.agent_runtime.errors import ToolError


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("run-tests")
    yield
    toolspec.session_var.reset(token)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text("def test_a():\n    assert 1 + 1 == 2\n\ndef test_b():\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_bad.py").write_text(
        "import pytest\n\ndef helper(x):\n    return x * 2\n\ndef test_wrong():\n    assert helper(2) == 5\n\n"
        "@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n):\n    assert n\n", encoding="utf-8")
    return root.resolve()


def run(ws, **inp):
    return asyncio.run(tools.execute_tool("run_tests", inp, workspace=ws))


def test_the_tool_is_registered_as_a_non_read_only_execute_tool():
    spec = toolspec.spec_for("run_tests")
    assert spec.permission == "execute" and not spec.read_only and not spec.concurrency_safe
    assert "run_tests" in {s["name"] for s in tools.all_tool_schemas()}


def test_passing_tests_report_counts(ws):
    out = run(ws, paths=["tests/test_ok.py"])
    assert out.startswith("PASSED: 2 passed")


def test_a_failure_reports_counts_the_short_traceback_and_the_id(ws):
    out = run(ws, paths=["tests/test_bad.py"])
    assert out.startswith("FAILED: ") and "1 failed" in out
    assert "assert helper(2) == 5" in out and "FAILED tests/test_bad.py::test_wrong" in out


def test_a_node_id_with_a_parameter_runs_just_that_test(ws):
    out = run(ws, paths=["tests/test_bad.py::test_param[1]"])
    assert out.startswith("PASSED: 1 passed")


def test_a_missing_test_is_reported_not_raised(ws):
    out = run(ws, paths=["tests/test_ok.py::test_nope"])
    assert out.startswith("ERROR") or out.startswith("FAILED") or out.startswith("NO TESTS")


@pytest.mark.parametrize("bad", [
    "tests/test_ok.py; echo hi", "tests/test_ok.py && dir", "tests/test_ok.py | more", "$(whoami)", "`id`", "tests/test_ok.py > out",
    "-k foo", "--co", "-p", "../outside.py", "tests/../../x.py", "/etc/passwd", "C:" + chr(92) + "Windows" + chr(92) + "x.py", "C:/Windows/x.py", "tests" + chr(92) + ".." + chr(92) + "x.py",
    "tests/test_ok.py::test_a name", "tests/test_ok.py::test_a'", "tests/*.py", "%PATH%", "",
])
def test_arguments_that_are_not_test_paths_are_refused(ws, bad):
    with pytest.raises(ToolError):
        run(ws, paths=[bad])


def test_other_shapes_of_paths_are_refused(ws):
    for bad in (None, [], "", [1], ["tests/test_missing.py"], ["tests/data.txt"], ["tests/test_ok.py"] * 21):
        if bad == ["tests/data.txt"]:
            (ws / "tests" / "data.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ToolError):
            asyncio.run(tools.execute_tool("run_tests", {"paths": bad}, workspace=ws))


def test_a_path_through_a_link_out_of_the_workspace_is_refused(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    link = ws / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted here")
    with pytest.raises(ToolError, match="outside"):
        run(ws, paths=["linked/test_x.py"])


def test_the_command_is_exactly_pytest_with_quiet_and_stop_at_first_failure():
    cmd = run_tests.build_command(["tests/test_a.py::test_b[1-x]"])
    assert cmd.startswith("python -m pytest -q -x ") and "tests/test_a.py::test_b[1-x]" in cmd


def test_it_starts_the_process_through_the_configured_sandbox(ws, monkeypatch):
    seen = []
    real = sandbox.start

    async def spy(command, cwd, workspace):
        seen.append((command, cwd))
        return await real(command, cwd, workspace)

    monkeypatch.setattr(sandbox, "start", spy)
    run(ws, paths=["tests/test_ok.py"])
    assert len(seen) == 1 and seen[0][0].startswith("python -m pytest") and seen[0][1] == ws


def test_the_timeout_comes_from_config_and_stops_the_run(ws, monkeypatch):
    (ws / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(run_tests, "timeout_s", lambda: 2)
    with pytest.raises(ToolError, match="timed out after 2s"):
        run(ws, paths=["tests/test_slow.py"])


def test_timeout_setting_is_read_and_clamped(monkeypatch):
    monkeypatch.setattr(run_tests, "_config", lambda: {})
    assert run_tests.timeout_s() == 300
    monkeypatch.setattr(run_tests, "_config", lambda: {"timeout_s": 45})
    assert run_tests.timeout_s() == 45
    monkeypatch.setattr(run_tests, "_config", lambda: {"timeout_s": 10 ** 6})
    assert run_tests.timeout_s() == run_tests.MAX_TIMEOUT_S
    monkeypatch.setattr(run_tests, "_config", lambda: {"timeout_s": "x"})
    assert run_tests.timeout_s() == 300


def test_summarise_on_a_collection_error_keeps_the_tail():
    out = run_tests.summarise("ERROR collecting tests/t.py\nImportError: no module named zzz\n\n[exit code 2]")
    assert out.startswith("ERROR:") and "ImportError" in out


@pytest.mark.parametrize("make", [
    lambda ws: {"paths": ["./tests/test_ok.py"]},
    lambda ws: {"paths": ["tests" + chr(92) + "test_ok.py"]},
    lambda ws: {"paths": "tests/test_ok.py"},
    lambda ws: {"paths": ["tests/test_ok.py:3"]},
    lambda ws: {"paths": '"tests/test_ok.py"'},
    lambda ws: {"test_paths": ["tests/test_ok.py"]},
    lambda ws: {"tests": "tests/test_ok.py"},
    lambda ws: {"target": "tests/test_ok.py"},
    lambda ws: {"paths": "python -m pytest tests/test_ok.py"},
    lambda ws: {"paths": [str(ws / "tests" / "test_ok.py")]},
    lambda ws: {"paths": ["tests"], "k": "test_a"},
])
def test_reasonable_forms_are_accepted(ws, make):
    assert run(ws, **make(ws)).startswith("PASSED")


def test_a_string_with_several_targets_is_split(ws):
    assert run_tests.clean_targets(ws, "tests/test_ok.py, tests/test_bad.py::test_param[1]") == [
        "tests/test_ok.py", "tests/test_bad.py::test_param[1]"]


def test_k_selects_tests_and_is_validated(ws):
    out = run(ws, paths=["tests/test_ok.py"], k="test_b")
    assert out.startswith("PASSED: 1 passed")
    assert run_tests.build_command(["tests/a.py"], "a and not b").count("-k") == 1
    for bad in ("a; rm x", "$(id)", "-x", "a`b`", "a|b", "%PATH%", "a" * 201, 5):
        with pytest.raises(ToolError, match="k"):
            run(ws, paths=["tests/test_ok.py"], k=bad)


def test_paths_outside_the_workspace_are_still_refused(ws, tmp_path):
    (tmp_path / "test_out.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    for bad in (str(tmp_path / "test_out.py"), "../test_out.py", "tests/../../test_out.py", "/test_out.py"):
        with pytest.raises(ToolError):
            run(ws, paths=[bad])


def test_refusals_say_the_accepted_shape_and_suggest_a_near_file(ws):
    with pytest.raises(ToolError) as e:
        run(ws, paths=["test_ok.py"])
    assert "tests/test_ok.py" in str(e.value) and '"paths"' in str(e.value)
    for bad in (["-x"], ["a;b"], [], ["tests/test_ok.py::a b"]):
        with pytest.raises(ToolError, match='"paths"'):
            run(ws, paths=bad)
