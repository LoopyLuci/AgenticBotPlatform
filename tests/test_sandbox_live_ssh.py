"""The ssh sandbox backend against a REAL OpenSSH server (roadmap P2).

test_sandbox_ssh.py drives the backend through a fake `ssh` program. This file starts
a real sshd (Alpine's openssh-server, in a throwaway container on 127.0.0.1) and runs
the real `ssh` client against it, with key auth, BatchMode, and a host key pinned in a
private known_hosts file, the same trust setup the backend requires in production.
Skipped unless a real Docker daemon is reachable and the image builds; never faked.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
import uuid

import pytest

from bot.agent_runtime import sandbox, toolspec, tools
from bot.agent_runtime.errors import ToolError

IMAGE = "abp-test-sshd:1"
DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache openssh-server util-linux-misc procps \\
 && ssh-keygen -A \\
 && adduser -D -s /bin/sh abp \\
 && echo "abp:$(head -c 24 /dev/urandom | base64)" | chpasswd \\
 && mkdir -p /home/abp/.ssh /home/abp/ws/sub \\
 && chown -R abp:abp /home/abp \\
 && sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
CMD ["/usr/sbin/sshd", "-D", "-e"]
"""


def _run(*argv, **kw):
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)


def _docker() -> str | None:
    docker = shutil.which("docker")
    if not docker or not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        return None
    try:
        return docker if _run(docker, "version", timeout=15).returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


DOCKER = _docker()
pytestmark = [pytest.mark.xdist_group("live-ssh"),   # one sshd container, not one per worker
              pytest.mark.skipif(not DOCKER, reason="needs a real Docker daemon plus ssh and ssh-keygen")]


@pytest.fixture(scope="module")
def sshd(tmp_path_factory):
    """A running sshd; yields the ssh sandbox settings that reach it."""
    if _run(DOCKER, "image", "inspect", IMAGE, timeout=30).returncode != 0:
        built = subprocess.run([DOCKER, "build", "-t", IMAGE, "-"], input=DOCKERFILE, capture_output=True,
                               text=True, timeout=600)
        if built.returncode != 0:
            pytest.skip(f"could not build the test sshd image: {built.stderr[-500:]}")
    keys = tmp_path_factory.mktemp("ssh")
    key = keys / "id_ed25519"
    assert _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "abp-live-test", "-f", str(key)).returncode == 0
    name = f"abp-test-sshd-{uuid.uuid4().hex[:8]}"
    started = _run(DOCKER, "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::22", IMAGE)
    assert started.returncode == 0, started.stderr
    try:
        pub = key.with_suffix(".pub").read_text().strip()
        _run(DOCKER, "exec", "-e", f"KEY={pub}", name, "sh", "-c",
             'echo "$KEY" > /home/abp/.ssh/authorized_keys && chown abp:abp /home/abp/.ssh/authorized_keys '
             '&& chmod 600 /home/abp/.ssh/authorized_keys')
        port = int(_run(DOCKER, "port", name, "22/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        host_key = _run(DOCKER, "exec", name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub").stdout.split()
        known = keys / "known_hosts"
        known.write_text(f"[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n")
        settings = {"host": "127.0.0.1", "port": port, "user": "abp", "identity_file": str(key),
                    "remote_workspace_root": "/home/abp/ws", "connect_timeout": 10,
                    "extra_args": ["-F", "none", "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=yes",
                                   "-o", "IdentitiesOnly=yes"]}
        deadline = time.monotonic() + 30
        while True:                                          # sshd takes a moment to accept connections
            probe = _run("ssh", "-o", "BatchMode=yes", "-p", str(port), "-i", str(key), *settings["extra_args"],
                         "abp@127.0.0.1", "true", timeout=20)
            if probe.returncode == 0:
                break
            assert time.monotonic() < deadline, f"the test sshd never accepted the key: {probe.stderr}"
            time.sleep(0.5)
        yield {"container": name, "ssh": settings, "known_hosts": known}
    finally:
        _run(DOCKER, "rm", "-f", name, timeout=30)


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("live-ssh-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch, sshd):
    values: dict = {"backend": "ssh", "ssh": dict(sshd["ssh"])}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


def test_a_command_runs_on_the_real_host_as_the_configured_user(ws, cfg):
    out = run_shell(ws, "echo live-ssh-ok; id -un; pwd", timeout=60)
    assert "live-ssh-ok" in out and "[exit code 0]" in out
    assert "abp" in out and "/home/abp/ws" in out


def test_the_working_folder_is_translated_onto_the_remote_root(ws, cfg):
    out = run_shell(ws, "pwd", cwd="sub", timeout=60)
    assert "/home/abp/ws/sub" in out


def test_a_failing_command_reports_its_real_exit_code(ws, cfg):
    out = run_shell(ws, "exit 7", timeout=60)
    assert "[exit code 7]" in out


def test_a_timeout_kills_the_remote_process_too(ws, cfg, sshd):
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out after 3s"):
        run_shell(ws, "sleep 299", timeout=3)
    assert time.monotonic() - started < 30
    deadline = time.monotonic() + 10
    while True:
        left = _run(DOCKER, "exec", sshd["container"], "pgrep", "-f", "sleep 299").stdout.strip()
        if not left:
            break
        assert time.monotonic() < deadline, f"the remote command outlived the timeout: pids {left}"
        time.sleep(0.5)
    pidfiles = _run(DOCKER, "exec", sshd["container"], "sh", "-c", "ls -a /home/abp/ws | grep '^\\.abp-' || true")
    assert pidfiles.stdout.strip() == "", "the remote kill must also remove its pidfile"


def test_an_unknown_host_key_fails_closed(ws, cfg, tmp_path):
    wrong = tmp_path / "wrong_known_hosts"
    wrong.write_text("")
    cfg["ssh"]["extra_args"] = ["-F", "none", "-o", f"UserKnownHostsFile={wrong}", "-o", "StrictHostKeyChecking=yes"]
    out = run_shell(ws, "echo should-not-run", timeout=60)
    assert "should-not-run" not in out and "[exit code 0]" not in out


def test_network_none_never_runs_a_command_with_the_network(ws, cfg):
    # The container itself has a network. With network: none the command either runs in
    # an empty network namespace, or, when the host refuses to create one (Docker's
    # default seccomp profile does), never runs at all. Either way it gets no network.
    cfg["network"] = "none"
    out = run_shell(ws, "wget -q -T 5 -O /dev/null http://1.1.1.1/ && echo NET-REACHED; echo done", timeout=60)
    assert "NET-REACHED" not in out
