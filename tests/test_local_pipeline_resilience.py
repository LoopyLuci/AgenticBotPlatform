"""scripts/local_pipeline.py's self-healing: a flaky test is re-run once (and
reported), a real failure still blocks, a hung step times out, the release
gate's handshake skips the redundant re-run, and only one pipeline runs at a
time."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("local_pipeline_resilience", _SCRIPTS / "local_pipeline.py")
lp = importlib.util.module_from_spec(_SPEC)
sys.modules["local_pipeline_resilience"] = lp
_SPEC.loader.exec_module(lp)

import release_guard  # noqa: E402


def _script_runs(monkeypatch, outcomes):
    """Feeds check_python() a scripted sequence of (ok, output) per command."""
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append(cmd)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
    monkeypatch.setattr(lp, "_run", fake_run)
    return calls


def test_a_flaky_test_is_rerun_once_and_the_pipeline_continues(monkeypatch, capsys):
    calls = _script_runs(monkeypatch, [
        (True, ""),                                                  # compileall
        (False, "FAILED tests/test_x.py::test_flaky - assert 1"),    # first pytest run
        (True, "1 passed in 0.1s"),                                  # the re-run of just that test
        (True, ""),                                                  # pip-audit
    ])
    assert lp.check_python() is True
    rerun = calls[2]
    assert rerun[-1] == "tests/test_x.py::test_flaky"                # only the failure, not the whole suite
    assert "FLAKY" in capsys.readouterr().out


def test_a_test_that_fails_again_still_blocks(monkeypatch):
    _script_runs(monkeypatch, [
        (True, ""),
        (False, "FAILED tests/test_x.py::test_real - assert 1"),
        (False, "FAILED tests/test_x.py::test_real - assert 1"),
    ])
    assert lp.check_python() is False


def test_a_large_number_of_failures_is_a_regression_not_a_flake(monkeypatch):
    many = "\n".join(f"FAILED tests/test_x.py::test_{i} - boom" for i in range(lp.FLAKY_RERUN_LIMIT + 1))
    calls = _script_runs(monkeypatch, [(True, ""), (False, many)])
    assert lp.check_python() is False
    assert len(calls) == 2                                           # compile + one pytest run, no re-run


def test_a_hung_step_is_killed_and_reported(monkeypatch):
    ok, out = lp._run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=1)
    assert ok is False and "timed out" in out
    ok, out = lp._run(["definitely-not-a-real-tool-xyz"])
    assert ok is False and "not found" in out


def test_the_release_gate_handshake_skips_the_whole_pipeline(monkeypatch, capsys):
    monkeypatch.setattr(release_guard, "pipeline_already_passed", lambda root: "0123456789abcdef")
    monkeypatch.setattr(lp, "_run_pipeline", lambda args: pytest.fail("must not re-run the pipeline"))
    monkeypatch.setattr(sys, "argv", ["local_pipeline.py"])
    assert lp.main() == 0
    assert "already verified" in capsys.readouterr().out


def test_a_second_pipeline_is_refused_while_one_is_running(monkeypatch, capsys):
    monkeypatch.setattr(release_guard, "pipeline_already_passed", lambda root: None)

    class Held:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise release_guard.Busy("another release run is active (pid 1)")

        def __exit__(self, *exc):
            pass
    monkeypatch.setattr(release_guard, "PipelineLock", Held)
    monkeypatch.setattr(lp, "_run_pipeline", lambda args: pytest.fail("must not run"))
    monkeypatch.setattr(sys, "argv", ["local_pipeline.py"])
    assert lp.main() == 1
    assert "another release run is active" in capsys.readouterr().err


def test_a_stale_staged_bundle_is_rebuilt_before_the_rust_check(monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append(cmd)
        return True, ""
    monkeypatch.setattr(lp, "_run", fake_run)
    monkeypatch.setattr(lp.shutil, "which", lambda name: "/usr/bin/cargo")
    monkeypatch.setattr(release_guard, "missing_bundle_resources", lambda root: ["stage/abp_cicd"])
    assert lp.check_rust() is True
    assert "stage_bundle.py" in str(calls[0])          # rebuilt first
    assert calls[1][:2] == ["cargo", "fmt"]            # then the normal checks


def test_a_failing_stage_rebuild_fails_the_rust_check(monkeypatch):
    monkeypatch.setattr(lp, "_run", lambda *a, **k: (False, "no venv"))
    monkeypatch.setattr(lp.shutil, "which", lambda name: "/usr/bin/cargo")
    monkeypatch.setattr(release_guard, "missing_bundle_resources", lambda root: ["stage/abp_cicd"])
    assert lp.check_rust() is False
