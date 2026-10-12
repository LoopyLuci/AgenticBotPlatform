"""The persistent shell behind run_shell(persistent=true): cd and variables carry over (roadmap P1).

Real shell processes. The PowerShell driver is exercised on Windows; the bash driver is written but
only runs where bash exists (POSIX), so those paths are marked and untested on this machine.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from bot.agent_runtime import persistent_shell, sandbox, shell, toolspec, tools
from bot.agent_runtime.errors import ToolError

WIN = os.name == "nt"
posix_only = pytest.mark.skipif(WIN, reason="needs bash")
PWD = "(Get-Location).Path" if WIN else "pwd"
SET_VAR = "$env:ABP_X = 'kept'" if WIN else "export ABP_X=kept"
GET_VAR = "$env:ABP_X" if WIN else 'echo "$ABP_X"'
_PY = f'& "{sys.executable}"' if WIN else f'"{sys.executable}"'


def pyc(ws, code: str, name: str = "snippet.py") -> str:
    """A command that runs `code` with this Python; a script file keeps shell quoting out of it."""
    path = ws / name
    path.write_text(code, encoding="utf-8")
    return f"{_PY} {path.name}"


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("stateful-a")
    persistent_shell.close_all()
    persistent_shell._notes.clear()
    yield
    persistent_shell.close_all()
    toolspec.session_var.reset(token)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    return root.resolve()


async def call(ws, command, **inp):
    inp.setdefault("persistent", True)
    return await tools.execute_tool("run_shell", {"command": command, **inp}, workspace=ws)


def scenario(coro_fn):
    """One loop per scenario (the shell lives on it); every process is closed before it ends."""
    async def main():
        try:
            return await coro_fn()
        finally:
            persistent_shell.close_all()
    return asyncio.run(main())


def test_cd_persists_between_calls(ws):
    async def go():
        await call(ws, "cd sub")
        return await call(ws, PWD)

    out = scenario(go)
    assert out.splitlines()[0].rstrip().endswith("sub")
    assert "[exit code 0" in out


def test_environment_variable_persists(ws):
    async def go():
        await call(ws, SET_VAR)
        return await call(ws, GET_VAR)

    assert scenario(go).splitlines()[0].strip() == "kept"


def test_exit_codes_are_reported_and_do_not_end_the_shell(ws):
    async def go():
        bad = await call(ws, pyc(ws, "import sys; sys.exit(3)"))
        await call(ws, SET_VAR)
        ok = await call(ws, GET_VAR)
        return bad, ok

    bad, ok = scenario(go)
    assert "[exit code 3," in bad
    assert ok.splitlines()[0].strip() == "kept"


def test_a_failing_command_in_powershell_or_bash_is_non_zero(ws):
    cmd = "Get-Item .\\nope-missing" if WIN else "ls nope-missing"
    out = scenario(lambda: call(ws, cmd))
    assert "[exit code 0," not in out


def test_output_that_looks_like_a_marker_cannot_end_the_call(ws):
    fake = "ABPEND" + "0" * 32
    code = f"print('{fake}|0|/fake/dir'); print('after-fake')"

    async def go():
        return await call(ws, pyc(ws, code))

    out = scenario(go)
    assert "after-fake" in out
    assert f"{fake}|0|/fake/dir" in out
    assert out.rstrip().endswith("]") and "[exit code 0, cwd " in out.splitlines()[-1]
    assert "/fake/dir" not in out.splitlines()[-1]


def test_a_following_call_is_not_polluted_by_the_previous_output(ws):
    async def go():
        await call(ws, pyc(ws, "print('one'); print('two')", "a.py"))
        return await call(ws, pyc(ws, "print('three')", "b.py"))

    out = scenario(go)
    assert out.startswith("three") and "one" not in out


def test_multiline_and_quotes_survive(ws):
    cmd = "$a = 'it''s'\n$b = \"two words\"\nWrite-Output \"$a $b\"" if WIN else "a=\"it's\"\nb='two words'\necho \"$a $b\""
    assert scenario(lambda: call(ws, cmd)).splitlines()[0].strip() == "it's two words"


def test_timeout_kills_the_tree_and_the_next_call_starts_fresh(ws):
    pidfile = ws / "child.pid"
    child = ("import os,time,subprocess,sys; "
             f"open(r'{pidfile}','w').write(str(os.getpid())); time.sleep(60)")

    async def go():
        await call(ws, SET_VAR)
        with pytest.raises(ToolError, match="timed out after 1s"):
            await call(ws, pyc(ws, child), timeout=1)
        assert not persistent_shell._sessions
        return await call(ws, GET_VAR)

    out = scenario(go)
    assert "new shell started" in out and "kept" not in out
    pid = int(pidfile.read_text())
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    if WIN:
        import subprocess

        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_leaving_the_workspace_is_refused_next_call_and_reset(ws):
    outside = ws.parent
    cd_out = f"cd '{outside}'" if WIN else f"cd '{outside}'"

    async def go():
        warned = await call(ws, cd_out)
        with pytest.raises(ToolError, match="left the working directory"):
            await call(ws, PWD)
        back = await call(ws, PWD)
        return warned, back

    warned, back = scenario(go)
    assert "outside the working directory" in warned
    assert back.splitlines()[0].strip().lower() == str(ws).lower()


def test_cwd_argument_enters_a_subfolder_and_must_stay_inside(ws):
    async def go():
        out = await call(ws, PWD, cwd="sub")
        with pytest.raises(ToolError, match="outside the working directory"):
            await call(ws, PWD, cwd="..")
        return out

    assert scenario(go).splitlines()[0].strip().endswith("sub")


def test_two_sessions_are_isolated(ws):
    async def go():
        await call(ws, SET_VAR)
        await call(ws, "cd sub")
        token = toolspec.session_var.set("stateful-b")
        try:
            var = await call(ws, GET_VAR)
            where = await call(ws, PWD)
        finally:
            toolspec.session_var.reset(token)
        mine = await call(ws, GET_VAR)
        return var, where, mine

    var, where, mine = scenario(go)
    assert "kept" not in var
    assert not where.splitlines()[0].strip().endswith("sub")
    assert mine.splitlines()[0].strip() == "kept"


def test_default_is_unchanged_each_call_is_a_fresh_shell(ws):
    # the stateless path runs in cmd.exe on Windows, sh elsewhere
    set_var, get_var, pwd = ("set ABP_X=kept", "echo [%ABP_X%]", "cd") if WIN else (SET_VAR, GET_VAR, PWD)

    async def go():
        await call(ws, set_var, persistent=False)
        await call(ws, "cd sub", persistent=False)
        var = await call(ws, get_var, persistent=False)
        where = await call(ws, pwd, persistent=False)
        return var, where

    var, where = scenario(go)
    assert "kept" not in var and "[exit code 0]" in var
    assert not where.splitlines()[0].strip().endswith("sub")
    assert not persistent_shell._sessions


def test_config_default_turns_it_on_and_an_explicit_false_wins(ws, monkeypatch):
    monkeypatch.setattr(persistent_shell, "_config", lambda: {"persistent": True})

    async def go():
        await tools.execute_tool("run_shell", {"command": SET_VAR}, workspace=ws)
        on = await tools.execute_tool("run_shell", {"command": GET_VAR}, workspace=ws)
        off = await tools.execute_tool("run_shell", {"command": GET_VAR, "persistent": False}, workspace=ws)
        return on, off

    on, off = scenario(go)
    assert on.splitlines()[0].strip() == "kept"
    assert "kept" not in off


def test_unsupported_sandbox_falls_back_with_a_note(ws, monkeypatch):
    monkeypatch.setattr(sandbox, "backend", lambda: "docker")
    monkeypatch.setattr(sandbox, "start", lambda *a, **k: _fake_start(ws))
    out = scenario(lambda: call(ws, "echo hi"))
    assert out.startswith("[note: running this call in a fresh shell, because the docker sandbox")
    assert "fell-back" in out and not persistent_shell._sessions


async def _fake_start(ws):
    return await asyncio.create_subprocess_shell("echo fell-back", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)


def test_network_none_falls_back_without_weakening(ws, monkeypatch):
    monkeypatch.setattr(sandbox, "network", lambda cfg=None: "none")
    monkeypatch.setattr(sandbox, "start", lambda *a, **k: _fake_start(ws))
    out = scenario(lambda: call(ws, "echo hi"))
    assert "network: none" in out.splitlines()[0] and not persistent_shell._sessions
    with pytest.raises(ToolError, match="can't host"):
        scenario(lambda: sandbox.start_persistent(["x"], ws))


def test_idle_sessions_are_closed(ws):
    async def go():
        await call(ws, SET_VAR)
        sess = next(iter(persistent_shell._sessions.values()))
        proc = sess.proc
        assert persistent_shell.reap_idle(now=sess.last_used + 1) == 0
        assert persistent_shell.reap_idle(now=sess.last_used + persistent_shell.idle_seconds() + 5) == 1
        await asyncio.wait_for(proc.wait(), 10)
        return await call(ws, GET_VAR)

    out = scenario(go)
    assert "sitting idle" in out and "kept" not in out


def test_closing_a_session_closes_its_shell(ws):
    async def go():
        await call(ws, SET_VAR)
        proc = next(iter(persistent_shell._sessions.values())).proc
        assert await shell.kill_session_jobs() == 0
        await asyncio.wait_for(proc.wait(), 10)
        return len(persistent_shell._sessions)

    assert scenario(go) == 0


def test_exit_in_the_command_ends_the_shell_and_the_next_call_recovers(ws):
    async def go():
        with pytest.raises(ToolError, match="the shell exited"):
            await call(ws, "exit 5")
        return await call(ws, "echo again")

    out = scenario(go)
    assert "new shell started" in out and "again" in out


def test_large_output_is_capped(ws):
    code = "import sys; sys.stdout.write('x' * 600000)"
    out = scenario(lambda: call(ws, pyc(ws, code)))
    assert "output capped" in out and "[exit code 0," in out
    assert len(out) < 450_000


def test_background_with_persistent_is_refused(ws):
    with pytest.raises(ToolError, match="can't be combined"):
        scenario(lambda: call(ws, "echo hi", background=True))


@posix_only
def test_bash_driver_keeps_cd_and_variables(ws):
    async def go():
        await call(ws, "cd sub; export ABP_X=kept")
        return await call(ws, 'pwd; echo "$ABP_X"')

    out = scenario(go)
    assert out.splitlines()[0].endswith("sub") and out.splitlines()[1] == "kept"
