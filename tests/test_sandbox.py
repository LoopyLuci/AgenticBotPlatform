"""Where commands run and what environment they get (roadmap P2). The docker backend is
exercised against a stand-in `docker` program that records its arguments and runs the
command locally - not against a real Docker daemon."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time

import pytest

from bot.agent_runtime import sandbox, secrets_guard, shell, toolspec, tools
from bot.agent_runtime.errors import ToolError

SECRET = "correct-horse-battery-staple-9999"


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("sandbox-test")
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


# ---- environment ------------------------------------------------------------------
ENV = {"PATH": "/bin", "HOME": "/home/x", "MY_API_KEY": SECRET, "DB_PASSWORD": "hunter2hunter2", "EDITOR": "vim",
       "KEEP_TOKEN": "some-token-value-123"}


def test_secrets_mode_removes_credential_shaped_variables():
    env = sandbox.build_env(ENV, {})
    assert "MY_API_KEY" not in env and "DB_PASSWORD" not in env and "KEEP_TOKEN" not in env
    assert env["PATH"] == "/bin" and env["EDITOR"] == "vim"


def test_allow_keeps_a_named_variable_and_minimal_mode_keeps_only_the_basics():
    env = sandbox.build_env(ENV, {"env": {"allow": ["KEEP_TOKEN"]}})
    assert env["KEEP_TOKEN"] == "some-token-value-123" and "MY_API_KEY" not in env
    minimal = sandbox.build_env(ENV, {"env": {"mode": "minimal", "allow": ["EDITOR"]}})
    assert set(minimal) == {"PATH", "HOME", "EDITOR"}
    assert sandbox.build_env(ENV, {"env": {"mode": "inherit"}}) == ENV


def test_set_injects_variables_and_resolves_references_and_registers_them_as_secrets():
    env = sandbox.build_env({"PATH": "/bin", "REAL_UPSTREAM": "injected-upstream-value"},
                            {"env": {"set": {"PLAIN": "1", "UPSTREAM": "${REAL_UPSTREAM}"}}})
    try:
        assert env["PLAIN"] == "1" and env["UPSTREAM"] == "injected-upstream-value"
        assert "[secret:UPSTREAM]" in secrets_guard.redact("x injected-upstream-value y", {})
    finally:
        secrets_guard.unregister("UPSTREAM")
    with pytest.raises(ToolError, match="not set on the server"):
        sandbox.build_env({}, {"env": {"set": {"X": "${MISSING_VAR}"}}})
    with pytest.raises(ToolError, match="mode must be"):
        sandbox.build_env({}, {"env": {"mode": "open"}})


def test_a_command_cannot_read_the_servers_credentials(ws, cfg, monkeypatch):
    monkeypatch.setenv("EVAL_SECRET_VALUE", SECRET)
    monkeypatch.setenv("VISIBLE_SETTING", "fine")
    code = 'python -c "import os; print(os.environ.get(\'EVAL_SECRET_VALUE\'), os.environ.get(\'VISIBLE_SETTING\'))"'
    exe = f'"{sys.executable}"' + code[len("python"):]
    out = run_shell(ws, exe)
    assert "None fine" in out and SECRET not in out
    cfg["env"] = {"mode": "inherit"}
    assert SECRET in run_shell(ws, exe)


def test_unknown_backend_is_refused(cfg):
    cfg["backend"] = "ssh"
    with pytest.raises(ToolError, match="must be one of"):
        sandbox.backend()


# ---- docker ------------------------------------------------------------------------
FAKE = '''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "run":
    host = args[args.index("-v") + 1].rsplit(":/workspace", 1)[0]
    rel = args[args.index("-w") + 1][len("/workspace"):].lstrip("/")
    cwd = os.path.join(host, rel) if rel else host
    sys.exit(subprocess.call(args[-1], shell=True, cwd=cwd))
sys.exit(0)
'''


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "fake_docker.py").write_text(FAKE)
    if os.name == "nt":
        (bindir / "docker.cmd").write_text(f'@echo off\r\n"{sys.executable}" "%~dp0fake_docker.py" %*\r\n')
    else:
        launcher = bindir / "docker"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_docker.py" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_docker_argv_is_locked_down(ws):
    sub = ws / "sub"
    sub.mkdir()
    argv = sandbox._docker_argv("ls -la", sub, ws, "abp-test", {}, ["TOKEN_A"])
    text = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"] and argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop ALL" in text and "--security-opt no-new-privileges" in text and "--pids-limit 256" in text
    assert argv[argv.index("-w") + 1] == "/workspace/sub" and f"{ws}:/workspace" in argv
    assert argv[argv.index("-e") + 1] == "TOKEN_A"
    assert argv[-4:] == ["python:3.11-slim", "sh", "-c", "ls -la"]


def test_docker_options_and_the_host_network_refusal(ws):
    cfg = {"docker": {"image": "my/image:1", "network": "bridge", "memory": "512m", "cpus": "1", "pids": 64,
                      "user": "1000:1000", "extra_args": ["--read-only"]}}
    argv = sandbox._docker_argv("true", ws, ws, "n", cfg, [])
    assert argv[argv.index("--network") + 1] == "bridge" and "--read-only" in argv and "my/image:1" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000" and argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("-w") + 1] == "/workspace"
    with pytest.raises(ToolError, match="host is not allowed"):
        sandbox._docker_argv("true", ws, ws, "n", {"docker": {"network": "host"}}, [])
    with pytest.raises(ToolError, match="outside the workspace"):
        sandbox._docker_argv("true", ws.parent, ws, "n", {}, [])


def test_docker_backend_fails_closed_when_docker_is_missing(ws, cfg, monkeypatch):
    cfg["backend"] = "docker"
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    marker = ws / "ran.txt"
    with pytest.raises(ToolError, match="command was not run"):
        run_shell(ws, f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"')
    assert not marker.exists(), "the command must not fall back to running on the host"


def test_docker_backend_runs_the_command_in_the_container_with_the_right_arguments(ws, cfg, fake_docker):
    cfg["backend"] = "docker"
    (ws / "sub").mkdir()
    where = "cd" if os.name == "nt" else "pwd"
    out = run_shell(ws, where, cwd="sub")
    assert out.strip().splitlines()[0].endswith("sub") and out.rstrip().endswith("[exit code 0]")
    run_args = [c for c in fake_docker() if c[0] == "run"][0]
    assert "--rm" in run_args and run_args[run_args.index("--network") + 1] == "none"
    assert run_args[run_args.index("-w") + 1] == "/workspace/sub" and run_args[-1] == where


def test_docker_backend_hands_only_injected_values_to_the_container_and_hides_host_secrets(ws, cfg, fake_docker, monkeypatch):
    monkeypatch.setenv("HOST_ONLY_SECRET", SECRET)
    monkeypatch.setenv("UPSTREAM_VALUE", "injected-upstream-value")
    cfg.update(backend="docker", env={"set": {"INJECTED": "${UPSTREAM_VALUE}"}})
    try:
        out = run_shell(ws, "echo %INJECTED%-%HOST_ONLY_SECRET%" if os.name == "nt" else "echo $INJECTED-$HOST_ONLY_SECRET")
        assert "injected-upstream-value" in out and SECRET not in out
        run_args = [c for c in fake_docker() if c[0] == "run"][0]
        assert run_args[run_args.index("-e") + 1] == "INJECTED" and "injected-upstream-value" not in run_args
    finally:
        secrets_guard.unregister("INJECTED")


def test_timing_out_a_docker_command_removes_the_container(ws, cfg, fake_docker):
    cfg["backend"] = "docker"
    started = time.monotonic()
    slow = "ping -n 30 127.0.0.1 > nul" if os.name == "nt" else "sleep 30"
    with pytest.raises(ToolError, match="timed out after 1s"):
        run_shell(ws, slow, timeout=1)
    assert time.monotonic() - started < 15
    calls = fake_docker()
    name = [c for c in calls if c[0] == "run"][0][calls[0].index("--name") + 1]
    assert ["rm", "-f", name] in calls
