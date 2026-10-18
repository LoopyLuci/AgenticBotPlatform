"""The VM tier: sandbox backend `wsl_ephemeral` (bot/agent_runtime/wsl_vm.py).

Unit tests run anywhere. The proof tests run a real throw-away WSL2 distro and are skipped
when WSL is absent.
"""
from __future__ import annotations

import asyncio
import io
import os
import socket
import tarfile
import threading
import time

import pytest

from bot.agent_runtime import sandbox, toolspec, tools, wsl_vm
from bot.agent_runtime.errors import ToolError

HAVE_WSL = wsl_vm.available()
# The real-WSL tests import a ~4 GB base distro per run (~60-70 s each), so they are OPT-IN: set
# ABP_RUN_WSL_TESTS=1 to run them. Skipped by default (incl. in the pre-push pipeline) so a push stays fast;
# the unit-level tests below (argv / wsl.conf builders, no real import) always run.
RUN_WSL = HAVE_WSL and os.environ.get("ABP_RUN_WSL_TESTS", "") not in ("", "0", "false", "no")
needs_wsl = pytest.mark.skipif(
    not RUN_WSL,
    reason="WSL not available" if not HAVE_WSL else "set ABP_RUN_WSL_TESTS=1 to run the slow real-WSL tests",
)


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("vm-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture(scope="session", autouse=True)
def _base():
    if HAVE_WSL:
        wsl_vm.ensure_base()


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {"backend": "wsl_ephemeral", "network": "none"}
    monkeypatch.setattr(sandbox, "_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command, **kw):
    kw.setdefault("timeout", 400)       # importing a fresh distro takes about a minute
    return asyncio.run(tools.execute_tool("run_shell", {"command": command, **kw}, workspace=ws))


def strays() -> list[str]:
    names = [n for n in wsl_vm.list_distros() if n.startswith(wsl_vm.NAME_PREFIX)]
    root = wsl_vm.vm_root()
    dirs = [p.name for p in root.iterdir()] if root.is_dir() else []
    return names + dirs


# ---- unit (no WSL) ----

def test_backend_registered():
    assert "wsl_ephemeral" in sandbox.BACKENDS


def test_wsl_conf_isolation_settings():
    conf = wsl_vm.WSL_CONF
    assert "[automount]\nenabled=false\nmountFsTab=false" in conf
    assert "[interop]\nenabled=false\nappendWindowsPath=false" in conf
    assert "[network]\ngenerateResolvConf=false" in conf


def test_guest_script_is_offline_and_in_work(tmp_path):
    s = wsl_vm.guest_script("echo hi", "sub", 512)
    assert "unshare -rn" in s and "cd /work/sub" in s and "ulimit -v 524288" in s
    assert "/mnt" not in s


def test_unavailable_wsl_fails_closed(cfg, ws, monkeypatch):
    monkeypatch.setattr(wsl_vm, "available", lambda: False)
    with pytest.raises(ToolError, match="WSL is not available"):
        asyncio.run(sandbox.start("echo hi", ws, ws))


def test_no_wsl_exe_raises_toolerror(monkeypatch):
    monkeypatch.setattr(wsl_vm, "wsl_exe", lambda: None)
    with pytest.raises(ToolError, match="wsl.exe was not found"):
        wsl_vm.list_distros()


def test_unpack_refuses_escapes_and_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in (("ok.txt", b"ok"), ("../evil.txt", b"x"), ("/abs.txt", b"x")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        ln = tarfile.TarInfo("link")
        ln.type = tarfile.SYMTYPE
        ln.linkname = "/etc/passwd"
        tf.addfile(ln)
    dest = tmp_path / "w"
    dest.mkdir()
    assert wsl_vm.unpack_to(buf.getvalue(), dest) == 1
    assert (dest / "ok.txt").read_text() == "ok"
    assert not (tmp_path / "evil.txt").exists() and not (dest / "link").exists()


def test_pick_base_skips_docker_desktop():
    assert wsl_vm.pick_base_distro(["docker-desktop", "Ubuntu"]) == "Ubuntu"
    with pytest.raises(ToolError):
        wsl_vm.pick_base_distro(["docker-desktop"])


# ---- proof (real WSL) ----

@needs_wsl
def test_workspace_round_trip(cfg, ws):
    (ws / "in.txt").write_text("hello")
    out = run_shell(ws, "cat in.txt && echo done > out.txt && mkdir -p d && echo n > d/n.txt")
    assert "hello" in out
    assert (ws / "out.txt").read_text().strip() == "done"
    assert (ws / "d" / "n.txt").read_text().strip() == "n"
    assert not strays()


@needs_wsl
def test_guest_cannot_read_host_secret(cfg, ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-VALUE-9917")
    drive = secret.drive[0].lower()
    rest = secret.as_posix()[len(secret.drive):]
    out = run_shell(ws, f"cat /mnt/{drive}{rest}; echo ---; ls -A /mnt/c /mnt/d /mnt/e /mnt/{drive} 2>&1; "
                        "echo ---; find /mnt/c /mnt/d /mnt/e /mnt/z /mnt/x -type f 2>/dev/null | wc -l")
    assert "TOPSECRET-VALUE-9917" not in out
    assert "Users" not in out and "Windows" not in out and "Projects" not in out
    # automount off: the drive mount points are empty (find counts zero files)
    assert out.split("---")[-1].split()[0] == "0"
    assert not strays()


class _Listener:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.hit = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.hit = True
            c.close()

    def close(self):
        self.sock.close()


@needs_wsl
def test_guest_has_no_network(cfg, ws):
    lis = _Listener()
    try:
        cmd = (f"for h in 127.0.0.1 $(ip -4 route 2>/dev/null | awk '/default/{{print $3}}'); do "
               f"timeout 5 sh -c 'exec 3<>/dev/tcp/'$h'/{lis.port}' 2>/dev/null && echo CONNECTED; done; "
               "(timeout 5 wget -qO- http://example.com || timeout 5 curl -s http://example.com) 2>/dev/null | head -c 50; "
               "echo end")
        out = run_shell(ws, cmd.replace("sh -c", "bash -c"))
        time.sleep(0.5)
        assert "CONNECTED" not in out and "Example Domain" not in out
        assert not lis.hit
        assert "end" in out
    finally:
        lis.close()
    assert not strays()


@needs_wsl
def test_interop_is_off(cfg, ws):
    out = run_shell(ws, "cmd.exe /c echo INTEROPWORKS 2>&1; echo rc=$?")
    assert "INTEROPWORKS" not in out
    assert "rc=0" not in out


@needs_wsl
def test_teardown_after_run(cfg, ws):
    run_shell(ws, "echo hi")
    assert not strays()


@needs_wsl
def test_teardown_after_timeout(cfg, ws):
    cfg["wsl_ephemeral"] = {"timeout": 20}
    t0 = time.time()
    out = run_shell(ws, "sleep 600; echo never")
    assert time.time() - t0 < 400
    assert "never" not in out
    assert not strays()


@needs_wsl
def test_teardown_after_error(cfg, ws):
    run_shell(ws, "exit 7")
    assert not strays()


@needs_wsl
def test_teardown_when_killed(cfg, ws):
    async def go():
        proc = await sandbox.start("sleep 600", ws, ws)
        await asyncio.sleep(100)        # past provisioning, so the guest command is running
        sandbox.kill(proc)
        await asyncio.sleep(1)
    asyncio.run(go())
    deadline = time.time() + 30
    while strays() and time.time() < deadline:
        time.sleep(1)
    assert not strays()
