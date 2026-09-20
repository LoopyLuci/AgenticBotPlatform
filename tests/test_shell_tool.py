"""run_shell's timeout, cwd and background jobs (roadmap P1)."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from bot.agent_runtime import shell, toolspec, tools
from bot.agent_runtime.errors import ToolError

PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("shell-test")
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run(coro):
    """One loop per scenario: a background job lives on the loop that started it."""
    return asyncio.run(coro)


async def call(ws, name, **inp):
    return await tools.execute_tool(name, inp, workspace=ws)


def test_foreground_command_returns_output_and_exit_code(ws):
    out = run(call(ws, "run_shell", command=f'{PY} -c "print(42)"'))
    assert out.strip().startswith("42") and out.rstrip().endswith("[exit code 0]")
    bad = run(call(ws, "run_shell", command=f'{PY} -c "import sys; sys.exit(3)"'))
    assert "[exit code 3]" in bad


def test_cwd_runs_in_a_subfolder_and_refuses_to_leave_the_workspace(ws):
    (ws / "sub").mkdir()
    out = run(call(ws, "run_shell", command=f'{PY} -c "import os; print(os.getcwd())"', cwd="sub"))
    assert out.strip().splitlines()[0].endswith("sub")
    with pytest.raises(ToolError, match="outside the working directory"):
        run(call(ws, "run_shell", command="echo hi", cwd=".."))
    with pytest.raises(ToolError, match="not a folder"):
        run(call(ws, "run_shell", command="echo hi", cwd="missing"))


def test_timeout_stops_the_command_and_reports_output_so_far(ws):
    code = "import time; print('started', flush=True); time.sleep(30)"
    with pytest.raises(ToolError, match=r"(?s)timed out after 1s.*started"):
        run(call(ws, "run_shell", command=f'{PY} -c "{code}"', timeout=1))


def test_timeout_is_validated_and_capped(ws):
    with pytest.raises(ToolError, match="at least 1"):
        run(call(ws, "run_shell", command="echo x", timeout=0))
    with pytest.raises(ToolError, match="number of seconds"):
        run(call(ws, "run_shell", command="echo x", timeout="soon"))
    assert shell._clean_timeout(99999) == shell.MAX_TIMEOUT_S


def test_empty_command_is_refused(ws):
    with pytest.raises(ToolError, match="can't be empty"):
        run(call(ws, "run_shell", command="   "))


def test_a_background_job_runs_while_the_agent_carries_on_and_can_be_read_and_stopped(ws):
    async def scenario():
        code = "import time\nprint('first', flush=True)\ntime.sleep(1.5)\nprint('second', flush=True)\ntime.sleep(60)"
        (ws / "job.py").write_text(code)
        started = await call(ws, "run_shell", command=f"{PY} job.py", background=True)
        assert "job" in started
        jid = started.split()[3]
        await asyncio.sleep(0.8)
        first = await call(ws, "shell_output", id=jid)
        assert "running" in first and "first" in first and "second" not in first
        listing = await call(ws, "shell_list")
        assert jid in listing and "running" in listing
        await asyncio.sleep(1.5)
        second = await call(ws, "shell_output", id=jid)
        assert "second" in second and "first" not in second           # only what is new
        assert "(no new output)" in await call(ws, "shell_output", id=jid)
        assert "Stopped" in await call(ws, "shell_kill", id=jid)
        assert "exited" in await call(ws, "shell_output", id=jid)
    run(scenario())


def test_a_finished_job_reports_its_exit_code(ws):
    async def scenario():
        started = await call(ws, "run_shell", command=f'{PY} -c "print(7)"', background=True)
        jid = started.split()[3]
        await asyncio.sleep(1.5)
        out = await call(ws, "shell_output", id=jid)
        assert "exited with code 0" in out and "7" in out
        assert "already" in await call(ws, "shell_kill", id=jid)
    run(scenario())


def test_jobs_belong_to_their_session_and_unknown_ids_are_errors(ws):
    async def scenario():
        started = await call(ws, "run_shell", command=f'{PY} -c "import time; time.sleep(30)"', background=True)
        jid = started.split()[3]
        other = toolspec.session_var.set("someone-else")
        try:
            with pytest.raises(ToolError, match="no background job"):
                await call(ws, "shell_output", id=jid)
            assert await call(ws, "shell_list") == "No background jobs."
        finally:
            toolspec.session_var.reset(other)
        await call(ws, "shell_kill", id=jid)
        with pytest.raises(ToolError, match="no background job"):
            await call(ws, "shell_kill", id="job999999")
    run(scenario())


def test_the_number_of_running_jobs_is_limited(ws, monkeypatch):
    monkeypatch.setattr(shell, "MAX_JOBS_PER_SESSION", 2)

    async def scenario():
        cmd = f'{PY} -c "import time; time.sleep(30)"'
        ids = [(await call(ws, "run_shell", command=cmd, background=True)).split()[3] for _ in range(2)]
        with pytest.raises(ToolError, match="already running"):
            await call(ws, "run_shell", command=cmd, background=True)
        for jid in ids:
            await call(ws, "shell_kill", id=jid)
    run(scenario())


def test_killing_a_job_stops_its_child_processes_too(ws):
    marker = ws / "alive.txt"
    (ws / "child.py").write_text(
        "import time, pathlib\n"
        "for i in range(200):\n"
        f"    pathlib.Path(r'{marker}').write_text(str(i))\n"
        "    time.sleep(0.1)\n")
    (ws / "parent.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "time.sleep(60)\n")

    async def scenario():
        jid = (await call(ws, "run_shell", command=f"{PY} parent.py", background=True)).split()[3]
        await asyncio.sleep(1.5)
        assert marker.exists()
        await call(ws, "shell_kill", id=jid)
        await asyncio.sleep(0.5)
        before = marker.read_text()
        await asyncio.sleep(0.8)
        assert marker.read_text() == before, "the grandchild kept running after the kill"
    run(scenario())


def test_cancelling_a_foreground_command_stops_it(ws):
    marker = ws / "tick.txt"
    code = f"import time,pathlib\nfor i in range(300):\n pathlib.Path(r'{marker}').write_text(str(i)); time.sleep(0.1)"
    (ws / "tick.py").write_text(code)

    async def scenario():
        task = asyncio.ensure_future(call(ws, "run_shell", command=f"{PY} tick.py"))
        await asyncio.sleep(1.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        before = marker.read_text()
        await asyncio.sleep(0.8)
        assert marker.read_text() == before
    run(scenario())


def test_run_shell_stays_approval_gated_and_the_job_tools_are_classified():
    assert tools.is_dangerous("run_shell")
    assert not tools.is_dangerous("shell_output") and not tools.is_dangerous("shell_list")
    assert not tools.is_dangerous("shell_kill")
    assert toolspec.spec_for("run_shell").max_output_chars == 12_000
