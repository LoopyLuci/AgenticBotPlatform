"""sandbox.network: none - every backend taken offline, enforced by the OS, fail closed.

The Windows tests run for real: an AppContainer with no capabilities (appcontainer.py).
The Linux path runs for real inside WSL when a distro is registered. What cannot be run
on this machine (macOS sandbox-exec, a Linux host's own unshare) is checked by argv.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import psutil
import pytest

from bot.agent_runtime import appcontainer, sandbox, toolspec, tools
from bot.agent_runtime.errors import ToolError

WINDOWS = os.name == "nt"
CURL = shutil.which("curl.exe") if WINDOWS else None


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("offline-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"backend": "local", "network": "none"}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    return root.resolve()


def run_shell(ws, command, **kw):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


class _Listener:
    """A loopback TCP server that records whether anything connected."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            try:
                conn.settimeout(5)
                conn.recv(65536)          # read the request first: closing with it unread sends a reset on Windows
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
            finally:
                conn.close()

    def close(self):
        self._stop = True
        self._t.join(timeout=2)
        self.sock.close()


@pytest.fixture
def listener():
    lst = _Listener()
    yield lst
    lst.close()


# ---- settings and argv, every platform ----------------------------------------------
def test_network_defaults_to_allow_and_rejects_unknown_values():
    assert sandbox.network({}) == "allow"
    assert sandbox.network({"network": "NONE"}) == "none"
    with pytest.raises(ToolError, match="sandbox.network must be one of"):
        sandbox.network({"network": "firewalled"})


def test_an_unknown_network_value_refuses_the_command(ws, cfg):
    cfg["network"] = "sometimes"
    with pytest.raises(ToolError, match="sandbox.network must be one of"):
        run_shell(ws, "echo should-not-run")


def test_docker_is_forced_offline_whatever_its_own_setting_says(ws):
    cfg = {"network": "none", "docker": {"network": "bridge"}}
    argv = sandbox._docker_argv("true", ws, ws, "abp-x", cfg, [])
    assert argv[argv.index("--network") + 1] == "none"
    argv = sandbox._docker_argv("true", ws, ws, "abp-x", {"docker": {"network": "bridge"}}, [])
    assert argv[argv.index("--network") + 1] == "bridge"


def test_ssh_and_wsl_run_the_command_in_a_fresh_network_namespace(ws):
    ssh = sandbox._ssh_argv("curl example.com", ws, ws, {"network": "none", "ssh": {"host": "h", "remote_workspace_root": "/w"}},
                            "/w/.p")
    assert "unshare -rn sh -c 'curl example.com'" in ssh[-1]
    wsl = sandbox._wsl_argv("curl example.com", ws, ws, {"network": "none"}, "/mnt/x/.p")
    assert "unshare -rn sh -c 'curl example.com'" in wsl[-1]
    plain = sandbox._ssh_argv("curl example.com", ws, ws, {"ssh": {"host": "h", "remote_workspace_root": "/w"}}, "/w/.p")
    assert "unshare" not in plain[-1]


def test_macos_uses_a_sandbox_exec_profile_that_denies_the_network(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
    prefix = sandbox._offline_posix_prefix()
    assert prefix[0] == "/usr/bin/sandbox-exec" and "(deny network*)" in prefix[2]
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="sandbox-exec was not found"):
        sandbox._offline_posix_prefix()


def test_linux_probes_unshare_once_and_falls_back_to_map_root(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_unshare_flags", None)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/unshare")
    probes = []

    def fake_run(argv, **kw):
        probes.append(argv)
        ok = "--map-root-user" in argv            # an older util-linux without --map-current-user
        return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "unrecognized option")
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox._offline_posix_prefix() == ["/usr/bin/unshare", "--user", "--map-root-user", "--net"]
    assert sandbox._offline_posix_prefix() == ["/usr/bin/unshare", "--user", "--map-root-user", "--net"]
    assert len(probes) == 2, "the probe result is cached"


def test_linux_refuses_when_user_namespaces_are_disabled(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_unshare_flags", None)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/unshare")
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "unshare: write failed: Operation not permitted"))
    with pytest.raises(ToolError, match="does not allow unprivileged network namespaces.*Operation not permitted"):
        sandbox._offline_posix_prefix()
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="unshare command was not found"):
        sandbox._offline_posix_prefix()


def test_the_launcher_argv_carries_limits_and_extra_paths(ws):
    argv = appcontainer.launcher_argv("py", 'echo "a b"', ws / "sub", ws, extra_paths=["C:/tools"], memory_mb=64,
                                      active_process_limit=3)
    assert argv[:2] == ["py", "-I"] and argv[-2:] == ["--", 'echo "a b"']
    assert argv[argv.index("--read") + 1] == "C:/tools"
    assert argv[argv.index("--memory-mb") + 1] == "64" and argv[argv.index("--procs") + 1] == "3"


def test_the_offline_setting_is_on_the_settings_page():
    from bot.agent_runtime import settings_schema

    ids = {f["id"]: f for f in settings_schema.FIELDS}
    assert [c[0] for c in ids["native_agent.sandbox.network"]["choices"]] == list(sandbox.NETWORKS)
    assert ids["native_agent.sandbox.network"]["default"] == "allow"
    assert "native_agent.sandbox.network_none.extra_paths" in ids


# ---- Windows: a real AppContainer ----------------------------------------------------
windows_only = pytest.mark.skipif(not WINDOWS, reason="AppContainers only exist on Windows")


@windows_only
class TestAppContainer:
    def test_a_command_runs_and_its_output_and_exit_code_come_back(self, ws, cfg):
        out = run_shell(ws, "echo offline-hello & exit 3")
        assert "offline-hello" in out and "[exit code 3]" in out

    def test_it_can_write_in_the_workspace(self, ws, cfg):
        out = run_shell(ws, "echo made-offline> note.txt", cwd="sub")
        assert "[exit code 0]" in out
        assert (ws / "sub" / "note.txt").read_text().strip() == "made-offline"

    @pytest.mark.skipif(not CURL, reason="curl.exe is not on this Windows")
    def test_even_loopback_is_unreachable(self, ws, cfg, listener):
        url = f"http://127.0.0.1:{listener.port}/"
        cfg["network"] = "allow"                              # control: the same command online does connect
        online = run_shell(ws, f"curl.exe -sS -m 5 {url}")
        assert "ok" in online and listener.connections == 1
        cfg["network"] = "none"
        offline = run_shell(ws, f"curl.exe -sS -m 5 {url}")
        assert listener.connections == 1, f"an offline command reached a loopback server: {offline!r}"
        assert "[exit code 0]" not in offline

    @pytest.mark.skipif(not CURL, reason="curl.exe is not on this Windows")
    def test_dns_and_the_internet_are_unreachable(self, ws, cfg):
        out = run_shell(ws, "curl.exe -sS -m 5 -o NUL https://example.com")
        assert "[exit code 0]" not in out

    def test_files_outside_the_workspace_are_out_of_reach(self, ws, cfg, tmp_path):
        secret = tmp_path / "outside.txt"
        secret.write_text("private-outside-the-workspace")
        out = run_shell(ws, f'type "{secret}"')
        assert "private-outside-the-workspace" not in out

    def test_extra_paths_are_readable_but_nothing_else_is(self, ws, cfg, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "readme.txt").write_text("extra-path-readable")
        assert "extra-path-readable" not in run_shell(ws, f'type "{tools_dir / "readme.txt"}"')
        cfg["network_none"] = {"extra_paths": [str(tools_dir)]}
        assert "extra-path-readable" in run_shell(ws, f'type "{tools_dir / "readme.txt"}"')
        out = run_shell(ws, f'echo x> "{tools_dir / "written.txt"}"')
        assert not (tools_dir / "written.txt").exists(), f"an extra path must be read-only: {out!r}"

    def test_a_timeout_kills_the_contained_tree(self, ws, cfg):
        marker = f"abp-offline-{uuid.uuid4().hex[:8]}"
        with pytest.raises(ToolError, match="timed out after 2s"):
            run_shell(ws, f"for /l %i in (1,1,2000000000) do @rem {marker}", timeout=2)
        deadline = time.monotonic() + 10
        while True:
            left = [p.pid for p in psutil.process_iter(["cmdline"]) if marker in " ".join(p.info["cmdline"] or [])]
            if not left:
                break
            assert time.monotonic() < deadline, f"processes outlived the timeout: {left}"
            time.sleep(0.3)

    def test_windows_job_limits_apply_inside_the_container(self, ws, cfg):
        cfg.update(backend="windows_job", windows_job={"active_process_limit": 1})
        out = run_shell(ws, "echo outer-ran & cmd /c echo nested-ran")
        assert "outer-ran" in out and "nested-ran" not in out

    def test_the_workspace_grant_is_applied_once(self, ws):
        sid = appcontainer.container_sid()
        try:
            assert appcontainer.grant(ws, sid, appcontainer.MODIFY) is True
            assert appcontainer.grant(ws, sid, appcontainer.MODIFY) is False
        finally:
            appcontainer._adv.FreeSid(sid)

    def test_a_launcher_failure_refuses_the_command(self, ws, cfg, tmp_path):
        missing = tmp_path / "no-such-folder"
        cfg["network_none"] = {"extra_paths": [str(missing)]}
        out = run_shell(ws, "echo should-not-run")
        assert "should-not-run" not in out.replace("echo should-not-run", "")
        assert "the command was not run" in out and "[exit code 126]" in out


# ---- Linux: a real network namespace, inside WSL -------------------------------------
def _wsl_distro() -> bool:
    if not WINDOWS or not shutil.which("wsl.exe"):
        return False
    try:
        r = subprocess.run(["wsl.exe", "-e", "sh", "-c", "command -v unshare"], capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _wsl_distro(), reason="no WSL distro with unshare on this machine")
class TestLinuxNamespaceInWsl:
    def test_an_offline_command_cannot_connect_but_still_runs(self, ws, cfg):
        cfg["backend"] = "wsl"
        probe = ("python3 -c \"import socket;s=socket.socket();s.settimeout(4);r=s.connect_ex(('1.1.1.1',53));"
                 "print('CONNECTED' if r == 0 else 'BLOCKED', r)\"")
        out = run_shell(ws, probe, timeout=60)
        assert "BLOCKED" in out and "CONNECTED" not in out
        assert not list(ws.glob(".abp-*.pid")), "the pidfile is removed when the command ends"
