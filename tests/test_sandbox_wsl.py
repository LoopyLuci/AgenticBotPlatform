"""The wsl sandbox backend (roadmap P2). Stand-in tests use a fake wsl.exe, same
pattern as fake_docker/fake_ssh. A live class also runs for real against whatever WSL
distro is actually registered on this machine (skipped, not faked, if none is)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from bot.agent_runtime import sandbox, toolspec, tools
from bot.agent_runtime.errors import ToolError


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("wsl-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"backend": "wsl"}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


def test_win_to_wsl_path_translation():
    assert sandbox._win_to_wsl_path("Z:\\Projects\\AgenticBotPlatform") == "/mnt/z/Projects/AgenticBotPlatform"
    assert sandbox._win_to_wsl_path("C:\\") == "/mnt/c"


def test_wsl_argv_is_built_correctly(ws):
    cfg = {"wsl": {"distro": "Ubuntu"}}
    argv = sandbox._wsl_argv("ls -la", ws, ws, cfg, f"{sandbox._win_to_wsl_path(ws)}/.abp-x.pid")
    assert argv[0] == "wsl.exe" and argv[1:3] == ["-d", "Ubuntu"]
    assert argv[-3:-1] == ["sh", "-c"]
    assert "ls -la" in argv[-1] and "echo $$" in argv[-1]


def test_wsl_backend_fails_closed_when_wsl_is_missing(ws, cfg, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    marker = ws / "ran.txt"
    with pytest.raises(ToolError, match="wsl.exe was not found"):
        run_shell(ws, f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"')
    assert not marker.exists()


# ---- fake wsl.exe, same shape as fake_docker/fake_ssh -------------------------------
FAKE = '''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_WSL_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
remote_cmd = args[-1]
# same posix-vs-cmd.exe problem as fake_ssh - pull the real command back out of
# "cd '<dir>' && echo $$ > '<pidfile>' && <command>" and run just that locally.
parts = remote_cmd.split(" && ", 2)
real_cmd = parts[2] if len(parts) == 3 else remote_cmd
cwd = os.environ.get("FAKE_WSL_CWD") or None
sys.exit(subprocess.call(real_cmd, shell=True, cwd=cwd))
'''


@pytest.fixture
def fake_wsl(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "fake_wsl.py").write_text(FAKE)
    if os.name == "nt":
        launcher = bindir / "wsl.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "%~dp0fake_wsl.py" %*\r\n')
    else:
        launcher = bindir / "wsl.exe"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_wsl.py" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "wsl.log"
    monkeypatch.setenv("FAKE_WSL_LOG", str(log))
    # Real wsl.exe (System32) has the exact literal name "wsl.exe" that sandbox.py
    # tries first, so it wins over any PATH-prepended fake with a different filename
    # (a .cmd can't be named "wsl.exe" - Windows refuses to run a non-PE file with a
    # .exe extension). Force the resolution instead of hoping PATH order sorts it out.
    real_which = shutil.which

    def fake_which(name, *a, **kw):
        if name in ("wsl.exe", "wsl"):
            return str(launcher)
        return real_which(name, *a, **kw)
    monkeypatch.setattr(sandbox.shutil, "which", fake_which)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_wsl_backend_runs_the_command_via_the_fake_client(ws, cfg, fake_wsl, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_CWD", str(ws))
    out = run_shell(ws, "echo hello-from-wsl")
    assert "hello-from-wsl" in out and "[exit code 0]" in out
    calls = fake_wsl()
    assert len(calls) == 1 and "echo hello-from-wsl" in calls[0][-1]


def test_timing_out_a_wsl_command_sends_a_remote_kill(ws, cfg, fake_wsl, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_CWD", str(ws))
    slow = "ping -n 30 127.0.0.1 > nul" if os.name == "nt" else "sleep 30"
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out after 1s"):
        run_shell(ws, slow, timeout=1)
    assert time.monotonic() - started < 15
    calls = fake_wsl()
    assert len(calls) == 2
    assert "kill -9" in calls[1][-1]


# ---- live, real WSL on this machine -------------------------------------------------
_HAS_WSL = bool(shutil.which("wsl.exe") or shutil.which("wsl"))


def _has_a_distro() -> bool:
    if not _HAS_WSL:
        return False
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode == 0 and out.stdout.strip(b"\x00\r\n ") != b""
    except Exception:
        return False


@pytest.mark.skipif(not _has_a_distro(), reason="no WSL distro registered on this machine")
class TestLiveWsl:
    def test_a_real_command_runs_in_the_default_distro(self, ws, cfg):
        marker_name = "abp_live_wsl_marker.txt"
        out = run_shell(ws, f"echo abp-live-wsl-check > {marker_name} && cat {marker_name}")
        assert "abp-live-wsl-check" in out
        assert (ws / marker_name).exists(), "the command must run with cwd translated onto the real workspace folder"
