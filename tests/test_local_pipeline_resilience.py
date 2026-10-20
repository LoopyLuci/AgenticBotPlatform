"""scripts/local_pipeline.py's self-healing: a flaky test is re-run serially with
the machine settled between attempts (and reported), a real failure that survives
every attempt still blocks, a hung step times out, the release gate's handshake
skips the redundant re-run, and only one pipeline runs at a time."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("local_pipeline_resilience", _SCRIPTS / "local_pipeline.py")
lp = importlib.util.module_from_spec(_SPEC)
sys.modules["local_pipeline_resilience"] = lp
_SPEC.loader.exec_module(lp)

import release_guard  # noqa: E402


@pytest.fixture(autouse=True)
def _no_difflint(monkeypatch):
    """These tests script check_python's commands; the advisory ML diff-lint (its own tests: test_difflint.py) stays off so it
    never looks for a real hub or GPU lock from here."""
    monkeypatch.setenv("ABP_PIPELINE_DIFFLINT", "0")


def _script_runs(monkeypatch, outcomes):
    """Feeds check_python() a scripted sequence of (ok, output) per command."""
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append(cmd)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
    monkeypatch.setattr(lp, "_run", fake_run)
    # The self-healing re-run really samples CPU and sleeps between attempts; stub that out so the
    # scripted unit tests stay instant and deterministic (the settle/telemetry behaviour is its own concern).
    monkeypatch.setattr(lp, "_settle", lambda attempt: {"cpu_pct": 0, "inference_ports_up": []})
    monkeypatch.setattr(lp, "_write_flaky_telemetry", lambda record: None)
    return calls


def test_a_flaky_test_is_rerun_once_and_the_pipeline_continues(monkeypatch, capsys):
    calls = _script_runs(monkeypatch, [
        (True, ""),                                                  # compileall
        (True, ""),                                                  # ruff
        (True, ""),                                                  # dev tools (xdist, pytest-cov) present
        (False, "FAILED tests/test_x.py::test_flaky - assert 1"),    # first pytest run
        (True, "1 passed in 0.1s"),                                  # the re-run of just that test
        (True, ""),                                                  # pip-audit
    ])
    assert lp.check_python() is True
    assert "-n" in calls[3] and "--cov=bot" in calls[3]     # parallel, with the coverage floor
    rerun = calls[4]
    assert rerun[-1] == "tests/test_x.py::test_flaky"                # only the failure, not the whole suite
    assert "FLAKY" in capsys.readouterr().out


def test_an_errored_test_is_rerun_once_like_a_failed_one(monkeypatch, capsys):
    calls = _script_runs(monkeypatch, [
        (True, ""), (True, ""), (True, ""),
        (False, "ERROR tests/test_x.py::test_setup@g1 - sqlite3.OperationalError: database is locked"),
        (True, "1 passed in 0.1s"),
        (True, ""),
    ])
    assert lp.check_python() is True
    assert calls[4][-1] == "tests/test_x.py::test_setup"             # xdist group suffix stripped
    assert "-rfE" in calls[3]
    out = capsys.readouterr().out
    assert "FLAKY" in out and "tests/test_x.py::test_setup" in out


def test_a_flake_that_loses_the_first_rerun_but_passes_a_later_one_is_recovered(monkeypatch, capsys):
    """The self-heal is multi-attempt: a timing test that still loses the FIRST settled re-run (machine not
    yet calm) but passes a later attempt must be treated as a flake, not a hard failure."""
    outcomes = [
        (True, ""), (True, ""), (True, ""),
        (False, "FAILED tests/test_x.py::test_timing - timeout"),     # first full pytest run
        (False, "FAILED tests/test_x.py::test_timing - timeout"),     # self-heal attempt 1 — still loses
        (True, "1 passed in 0.1s"),                                   # self-heal attempt 2 — recovers
        (True, ""),                                                   # pip-audit
    ]
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append(cmd)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
    monkeypatch.setattr(lp, "_run", fake_run)
    monkeypatch.setattr(lp, "_settle", lambda attempt: {"cpu_pct": 0, "inference_ports_up": []})
    monkeypatch.setattr(lp, "_write_flaky_telemetry", lambda record: None)
    monkeypatch.setattr(lp, "FLAKY_RERUN_ATTEMPTS", 3)
    assert lp.check_python() is True
    assert "FLAKY" in capsys.readouterr().out


def test_a_check_that_itself_crashes_is_reported_as_failed_not_propagated(monkeypatch):
    """An unexpected exception out of a check (a bug in the check, not a test) is caught, dumped, and
    reported as a clean failure — the pipeline must not abort with a bare traceback."""
    monkeypatch.setattr(lp, "_dump_crash", lambda *a, **k: None)

    def boom():
        raise RuntimeError("kaboom")
    assert lp._timed("explodey", boom) is False


def test_an_error_that_repeats_still_blocks_and_names_the_test(monkeypatch, capsys):
    _script_runs(monkeypatch, [
        (True, ""), (True, ""), (True, ""),
        (False, "ERROR tests/test_x.py::test_setup - boom"),
        (False, "ERROR tests/test_x.py::test_setup - boom"),
    ])
    assert lp.check_python() is False
    assert "tests/test_x.py::test_setup" in capsys.readouterr().out


def test_a_collection_error_is_not_rerun(monkeypatch):
    calls = _script_runs(monkeypatch, [(True, ""), (True, ""), (True, ""), (False, "ERROR tests/test_broken.py - ImportError")])
    assert lp.check_python() is False
    assert len(calls) == 4


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


def _stamp_env(monkeypatch, *, heads, dirty=(), code=0):
    """_run_pipeline with the body stubbed: records clear/stamp calls against a scripted HEAD sequence."""
    calls = []
    seq = iter(heads)
    monkeypatch.setattr(release_guard, "clear_pipeline_passed", lambda root: calls.append("clear"))
    monkeypatch.setattr(release_guard, "head_sha", lambda root: next(seq))
    monkeypatch.setattr(release_guard, "tracked_changes_outside", lambda root: list(dirty))
    monkeypatch.setattr(release_guard, "record_pipeline_passed", lambda root, sha: calls.append(("stamp", sha)))
    monkeypatch.setattr(lp, "_run_pipeline_body", lambda args: code)
    return calls


def test_a_green_deployed_run_records_its_exact_commit(monkeypatch):
    calls = _stamp_env(monkeypatch, heads=["abc123", "abc123"])
    assert lp._run_pipeline(SimpleNamespace(no_deploy=False)) == 0
    assert calls == ["clear", ("stamp", "abc123")]           # cleared first, then stamped


def test_a_failed_run_clears_and_never_stamps(monkeypatch):
    calls = _stamp_env(monkeypatch, heads=["abc123", "abc123"], code=1)
    assert lp._run_pipeline(SimpleNamespace(no_deploy=False)) == 1
    assert calls == ["clear"]


def test_a_no_deploy_run_is_not_stamped(monkeypatch):
    # push == deploy: a check-only run must not let a push ship code that was never deployed
    calls = _stamp_env(monkeypatch, heads=["abc123", "abc123"])
    assert lp._run_pipeline(SimpleNamespace(no_deploy=True)) == 0
    assert calls == ["clear"]


def test_no_stamp_when_head_moved_or_the_tree_was_dirty(monkeypatch):
    calls = _stamp_env(monkeypatch, heads=["abc123", "def456"])
    assert lp._run_pipeline(SimpleNamespace(no_deploy=False)) == 0
    assert calls == ["clear"]
    calls = _stamp_env(monkeypatch, heads=["abc123", "abc123"], dirty=["bot/x.py"])
    assert lp._run_pipeline(SimpleNamespace(no_deploy=False)) == 0
    assert calls == ["clear"]


def test_a_stamp_failure_never_turns_a_green_run_red(monkeypatch):
    _stamp_env(monkeypatch, heads=["abc123", "abc123"])

    def boom(root, sha):
        raise OSError("read-only git dir")
    monkeypatch.setattr(release_guard, "record_pipeline_passed", boom)
    assert lp._run_pipeline(SimpleNamespace(no_deploy=False)) == 0


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


# ---- the ML diff-lint runs after the live stop: it brings its own hub ------------------------------------------------------
def _difflint_on(monkeypatch, run):
    from bot.conductor import difflint

    monkeypatch.setenv("ABP_PIPELINE_DIFFLINT", "1")
    monkeypatch.setattr(lp, "_DIFF_RANGE", "aaa..bbb")
    monkeypatch.setattr(difflint, "run", run)
    return difflint


@pytest.mark.parametrize("env, want", [(None, True), ("0", False)])
def test_the_diff_lint_starts_its_own_hub_unless_told_not_to(monkeypatch, env, want):
    """Live ABP (and the hub its supervisor keeps up) is stopped before the gates, so the stage asks for a hub of its own."""
    from bot.conductor.difflint import Report

    seen = {}

    def run(root, base, head, budget=None, start_hub=False, **kw):
        seen["start_hub"] = start_hub
        return Report(status="ok")
    _difflint_on(monkeypatch, run)
    if env is None:
        monkeypatch.delenv("ABP_PIPELINE_DIFFLINT_HUB", raising=False)
    else:
        monkeypatch.setenv("ABP_PIPELINE_DIFFLINT_HUB", env)
    job = lp._start_difflint()
    job.thread.join(5)
    assert seen == {"start_hub": want}


def test_an_overrunning_diff_lint_has_its_hub_stopped_before_the_redeploy(monkeypatch, capsys):
    import threading

    from bot.conductor.difflint import Report

    gate = threading.Event()
    difflint = _difflint_on(monkeypatch, lambda *a, **k: gate.wait(5) and Report())
    stopped = []
    monkeypatch.setattr(difflint, "stop_own_hub", lambda: stopped.append(1))
    monkeypatch.setattr(difflint, "budget_s", lambda: 0.1)
    monkeypatch.setattr(lp, "DIFFLINT_JOIN_SLACK_S", 0.1)
    job = lp._start_difflint()
    lp._finish_difflint(job)
    gate.set()
    assert stopped == [1] and "overran" in capsys.readouterr().out


def test_a_diff_lint_that_starts_its_own_hub_does_not_hold_up_pytest(monkeypatch):
    """The stage is a side thread: pytest's command is issued while the stage is still busy bringing its hub up."""
    import threading

    from bot.conductor.difflint import Report

    release, done, at_pytest = threading.Event(), [], []

    def stage(*a, **k):
        release.wait(5)
        done.append(1)
        return Report(status="ok")
    _difflint_on(monkeypatch, stage)

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        if "pytest" in cmd and "-c" not in cmd:
            at_pytest.append(list(done))         # the stage has not finished when pytest starts
            release.set()
        return True, "1 passed in 0.1s"
    monkeypatch.setattr(lp, "_run", fake_run)
    monkeypatch.setattr(lp, "_keep_pytest_output", lambda out: None)
    assert lp.check_python() is True
    assert at_pytest and at_pytest[0] == [] and done == [1]
