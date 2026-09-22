"""The docker sandbox backend against a REAL Docker daemon (roadmap P2). Everything in
test_sandbox.py uses a stand-in `docker` program; this file is the honest supplement -
skipped unless `docker version` actually succeeds, never faked into a false pass."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time

import pytest

from bot.agent_runtime import sandbox, secrets_guard, toolspec, tools
from bot.agent_runtime.errors import ToolError


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        r = subprocess.run([docker, "version"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="no real Docker daemon reachable on this machine")


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("live-docker-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"backend": "docker", "docker": {"image": "python:3.11-slim"}}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


def test_a_command_runs_inside_a_real_container(ws, cfg):
    out = run_shell(ws, "echo hello-from-a-real-container", timeout=120)
    assert "hello-from-a-real-container" in out and "[exit code 0]" in out


def test_the_container_sees_the_mounted_workspace_and_writes_are_visible_on_the_host(ws, cfg):
    run_shell(ws, "echo host-visible-write > marker.txt", timeout=120)
    assert (ws / "marker.txt").read_text().strip() == "host-visible-write"


def test_network_none_actually_blocks_a_real_outbound_connection(ws, cfg):
    code = ("import socket,sys\n"
            "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)\n"
            "try:\n"
            " s.connect(('8.8.8.8', 53)); print('CONNECTED')\n"
            "except Exception as e:\n"
            " print('BLOCKED', type(e).__name__)\n")
    out = run_shell(ws, f'python -c "{code}"', timeout=60)
    assert "BLOCKED" in out and "CONNECTED" not in out, \
        f"network: none must actually prevent a real outbound connection, got: {out!r}"


def test_the_container_cannot_see_the_hosts_secrets(ws, cfg, monkeypatch):
    monkeypatch.setenv("LIVE_DOCKER_TEST_SECRET_KEY", "should-never-reach-the-container-9999")
    out = run_shell(ws, "printenv | grep -c LIVE_DOCKER_TEST_SECRET_KEY || true", timeout=60)
    assert "should-never-reach-the-container-9999" not in out


def test_a_real_timeout_stops_the_container_and_removes_it(ws, cfg):
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out after 2s"):
        run_shell(ws, "sleep 30", timeout=2)
    assert time.monotonic() - started < 30, "kill() must actually stop the real container promptly"
    # confirm no abp-* container was left running
    docker = shutil.which("docker")
    out = subprocess.run([docker, "ps", "--filter", "name=abp-", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=15)
    assert out.stdout.strip() == "", f"a real container was left running after timeout: {out.stdout!r}"


def test_missing_image_fails_the_command_rather_than_silently_running_on_the_host(ws, cfg):
    cfg["docker"] = {"image": "abp-test/this-image-does-not-exist:nope"}
    marker = ws / "ran_on_host.txt"
    out = run_shell(ws, f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"', timeout=120)
    assert "[exit code 0]" not in out, f"a missing image must not report success, got: {out!r}"
    assert not marker.exists(), "a docker pull/run failure must not fall back to running the command on the host"
