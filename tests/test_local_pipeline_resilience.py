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


def test_a_timed_out_steps_whole_process_tree_is_killed(monkeypatch, tmp_path):
    """A hung `docker info`/gradlew/cargo call can itself spawn helper
    processes - killing only the ONE process _run() started (what a plain
    subprocess.run(timeout=...) does) leaves those running. This is the
    real failure mode a live session hit: an orphaned docker.exe from an
    earlier interrupted run kept blocking every later `docker info` call.
    _run() must kill the whole tree, proven here with a real child process,
    not a mock."""
    marker = tmp_path / "child_still_running.txt"
    # The child writes its own PID, then sleeps well past the parent's timeout -
    # if it's still alive after _run() returns, the tree-kill didn't work.
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, time, sys\n"
        f"open(r'{marker}', 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, r'{child_script}'])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    ok, out = lp._run([sys.executable, str(parent_script)], timeout=2)
    assert ok is False and "timed out" in out

    import psutil
    child_pid = int(marker.read_text().strip())
    import time as _time
    _time.sleep(1)  # give the kill a moment to land
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_docker_info_uses_its_own_short_timeout_not_the_build_timeout(monkeypatch):
    """A Windows Docker Desktop backend hiccup made `docker info` hang for
    the full 30-minute BUILD_TIMEOUT in a real session, repeatedly wedging
    a push. The liveness check must never be able to do that again."""
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append((cmd, timeout))
        if cmd[:2] == ["docker", "info"]:
            return False, "timed out"
        return True, ""
    monkeypatch.setattr(lp, "_run", fake_run)
    monkeypatch.setattr(lp.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(lp.release_guard, "reap_stale_processes", lambda *a, **k: [])
    assert lp.check_docker() is None  # daemon unreachable -> skipped, not failed
    info_call = next(c for c in calls if c[0][:2] == ["docker", "info"])
    assert info_call[1] == lp.DOCKER_INFO_TIMEOUT
    assert lp.DOCKER_INFO_TIMEOUT < lp.BUILD_TIMEOUT


def test_check_docker_reaps_stale_docker_processes_before_pinging(monkeypatch):
    order = []
    monkeypatch.setattr(lp.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(lp.release_guard, "reap_stale_processes",
                         lambda names, **k: order.append("reap") or ["docker.exe(999)"])
    monkeypatch.setattr(lp, "_run", lambda *a, **k: order.append("run") or (False, ""))
    lp.check_docker()
    assert order == ["reap", "run"]  # reaped BEFORE the fresh docker info call, not after


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
