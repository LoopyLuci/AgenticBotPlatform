"""The ssh sandbox backend (roadmap P2). The stand-in tests exercise it against a fake
`ssh` program that records its arguments and runs the remote command locally, the same
pattern test_sandbox.py's fake_docker uses. A separate, explicitly-marked live class
only runs when ABP_TEST_SSH_HOST (and friends) name a real, already-trusted host -
skipped otherwise, never silently faked into a false pass."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time

import pytest

from bot.agent_runtime import sandbox, toolspec, tools
from bot.agent_runtime.errors import ToolError


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("ssh-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"backend": "ssh"}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


def test_ssh_backend_fails_closed_without_a_host(ws, cfg):
    cfg["ssh"] = {"remote_workspace_root": "/srv/ws"}
    with pytest.raises(ToolError, match="ssh.host is not set"):
        run_shell(ws, "echo hi")


def test_ssh_backend_fails_closed_without_a_remote_workspace_root(ws, cfg):
    cfg["ssh"] = {"host": "example.internal"}
    with pytest.raises(ToolError, match="remote_workspace_root is not set"):
        run_shell(ws, "echo hi")


def test_ssh_backend_fails_closed_when_ssh_is_missing(ws, cfg, monkeypatch):
    cfg["ssh"] = {"host": "example.internal", "remote_workspace_root": "/srv/ws"}
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    marker = ws / "ran.txt"
    with pytest.raises(ToolError, match="ssh command was not found"):
        run_shell(ws, f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"')
    assert not marker.exists()


def test_ssh_argv_is_built_correctly(ws):
    sub = ws / "sub"
    sub.mkdir()
    cfg = {"ssh": {"host": "h", "user": "u", "port": 2222, "identity_file": "id_rsa", "remote_workspace_root": "/srv/ws"}}
    argv = sandbox._ssh_argv("ls -la", sub, ws, cfg, "/srv/ws/.abp-x.pid")
    assert argv[0] == "ssh" and "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == "id_rsa"
    assert argv[-2] == "u@h"
    assert "cd /srv/ws/sub" in argv[-1] and "echo $$ > /srv/ws/.abp-x.pid" in argv[-1] and "ls -la" in argv[-1]


def test_ssh_argv_refuses_a_cwd_outside_the_workspace(ws):
    cfg = {"ssh": {"host": "h", "remote_workspace_root": "/srv/ws"}}
    with pytest.raises(ToolError, match="outside the workspace"):
        sandbox._ssh_argv("true", ws.parent, ws, cfg, "/srv/ws/.abp-x.pid")


# ---- fake ssh, same shape as test_sandbox.py's fake_docker -------------------------
FAKE = '''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
remote_cmd = args[-1]
# remote_cmd is "cd '<dir>' && echo $$ > '<pidfile>' && <command>" (posix syntax, not
# runnable by cmd.exe) - the actual command is only ever the third " && "-separated
# part in every command this test suite builds, so pull that back out and run just it
# locally, with the remote dir's Windows-side twin already prepared via FAKE_SSH_CWD.
parts = remote_cmd.split(" && ", 2)
real_cmd = parts[2] if len(parts) == 3 else remote_cmd
cwd = os.environ.get("FAKE_SSH_CWD") or None
sys.exit(subprocess.call(real_cmd, shell=True, cwd=cwd))
'''


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "fake_ssh.py").write_text(FAKE)
    if os.name == "nt":
        (bindir / "ssh.cmd").write_text(f'@echo off\r\n"{sys.executable}" "%~dp0fake_ssh.py" %*\r\n')
    else:
        launcher = bindir / "ssh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_ssh.py" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_ssh_backend_runs_the_remote_command_line_via_the_fake_client(ws, cfg, fake_ssh, monkeypatch):
    cfg["ssh"] = {"host": "h", "remote_workspace_root": "/srv/ws"}
    monkeypatch.setenv("FAKE_SSH_CWD", str(ws))
    out = run_shell(ws, "echo hello-from-remote")
    assert "hello-from-remote" in out and "[exit code 0]" in out
    calls = fake_ssh()
    assert len(calls) == 1 and calls[0][-2] == "h"
    assert "echo hello-from-remote" in calls[0][-1]


def test_ssh_kill_argv_targets_the_pidfile(ws):
    start_argv = ["ssh", "-o", "BatchMode=yes", "h", "cd '/srv/ws' && echo $$ > '/srv/ws/.abp-x.pid' && sleep 5"]
    kill_argv = sandbox._ssh_kill_argv(start_argv, "/srv/ws/.abp-x.pid")
    assert kill_argv[-2] == "h"
    assert "/srv/ws/.abp-x.pid" in kill_argv[-1] and "kill -9" in kill_argv[-1]


def test_timing_out_an_ssh_command_sends_a_remote_kill(ws, cfg, fake_ssh, monkeypatch):
    cfg["ssh"] = {"host": "h", "remote_workspace_root": "/srv/ws"}
    monkeypatch.setenv("FAKE_SSH_CWD", str(ws))
    slow = "ping -n 30 127.0.0.1 > nul" if os.name == "nt" else "sleep 30"
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out after 1s"):
        run_shell(ws, slow, timeout=1)
    assert time.monotonic() - started < 15
    calls = fake_ssh()
    assert len(calls) == 2, "expected one call to start the command and one best-effort remote kill"
    assert "kill -9" in calls[1][-1]


# ---- live, opt-in: a real, already-trusted SSH host --------------------------------
_LIVE_HOST = os.environ.get("ABP_TEST_SSH_HOST")


@pytest.mark.skipif(not _LIVE_HOST, reason="set ABP_TEST_SSH_HOST (and ABP_TEST_SSH_REMOTE_ROOT) to run this against a real host")
class TestLiveSsh:
    def test_a_real_command_runs_on_the_real_host(self, ws, cfg):
        cfg["ssh"] = {"host": _LIVE_HOST, "user": os.environ.get("ABP_TEST_SSH_USER", ""),
                      "remote_workspace_root": os.environ.get("ABP_TEST_SSH_REMOTE_ROOT", "/tmp")}
        out = run_shell(ws, "echo abp-live-ssh-check")
        assert "abp-live-ssh-check" in out
