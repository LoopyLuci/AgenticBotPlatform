"""Adversarial stress of the confined leaf-worker sandbox: filesystem-escape, persistence and
network-escape attempts must all be denied, and a bounded process spawn must be killed with no
orphans (the process-explosion guard). Mirrors test_sandbox_offline.py's real-run style; every
command runs for real on Windows through the confined override. Bounded on purpose (short
timeouts, few processes) so it is safe in CI."""
import asyncio
import ctypes
import os
import pathlib
import shutil
import string
import sys
import tempfile
import time
import uuid

import pytest

from bot.agent_runtime import sandbox, tools
from bot.agent_runtime.errors import ToolError

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="the native AppContainer sandbox is Windows-only")
CURL = shutil.which("curl.exe") if WINDOWS else None


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    return root.resolve()


def sh(ws, command, timeout=12):
    """run_shell under the confined override; a timeout/kill is returned as text, not raised."""
    with sandbox.confined_override():
        try:
            return asyncio.run(tools.execute_tool("run_shell", {"command": command, "timeout": timeout}, workspace=ws))
        except ToolError as exc:
            return f"[STOPPED: {exc}]"


@windows_only
def test_legit_workspace_write_is_allowed(ws):
    sh(ws, "cmd /c echo legit> sub\\legit.txt", timeout=8)
    assert (ws / "sub" / "legit.txt").read_text().strip() == "legit"


@windows_only
def test_secret_outside_workspace_is_unreadable(ws):
    secret = pathlib.Path(tempfile.gettempdir()) / f"stress_secret_{uuid.uuid4().hex}.txt"
    secret.write_text("TOP-SECRET-OUTSIDE-WS")
    try:
        assert "TOP-SECRET-OUTSIDE-WS" not in sh(ws, f'type "{secret}"')
    finally:
        secret.unlink(missing_ok=True)


@windows_only
def test_write_into_user_profile_ssh_is_denied(ws):
    target = pathlib.Path(os.environ["USERPROFILE"]) / ".ssh" / f"PWN_{uuid.uuid4().hex}.txt"
    sh(ws, f'cmd /c echo pwned> "{target}"')
    assert not target.exists()


@windows_only
def test_write_to_other_drive_root_is_denied(ws):
    # The system drive plus every other existing drive root, found at run time (no drive letters hard-coded:
    # the public-repo leak guard treats them as machine paths). At least as strict as the fixed pair it replaces.
    system = os.environ.get("SystemDrive", "C:").rstrip("\\/") + "/"
    fixed = 3                                                  # DRIVE_FIXED: skip network/optical/removable drives
    others = [f"{d}:/" for d in string.ascii_uppercase
              if f"{d}:/" != system and ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == fixed]
    for drive in [system, *others]:
        target = pathlib.Path(drive) / f"PWN_{uuid.uuid4().hex}.txt"
        sh(ws, f'cmd /c echo x> "{target}"')
        assert not target.exists()


@windows_only
def test_junction_to_home_write_escape_is_denied(ws):
    # Make a junction inside the workspace pointing at HOME, then try to write through it.
    home = os.environ["USERPROFILE"]
    through = pathlib.Path(home) / f"PWN_viaJ_{uuid.uuid4().hex}.txt"
    sh(ws, f'cmd /c mklink /J "escape_link" "{home}"')
    sh(ws, f'cmd /c echo x> "escape_link\\{through.name}"')
    assert not through.exists()


@windows_only
def test_temp_is_redirected_away_from_user_temp(ws):
    marker = f"PWN_temp_{uuid.uuid4().hex}.txt"
    sh(ws, f'cmd /c echo x> "%TEMP%\\{marker}"')
    assert not (pathlib.Path(tempfile.gettempdir()) / marker).exists()


@windows_only
def test_registry_run_persistence_is_denied(ws):
    key = f"ABP_STRESS_{uuid.uuid4().hex[:8]}"
    run = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"
    sh(ws, f'reg add {run} /v {key} /t REG_SZ /d calc.exe /f')
    try:
        assert key not in os.popen(f'reg query {run} /v {key} 2>&1').read()
    finally:
        os.system(f'reg delete {run} /v {key} /f >NUL 2>&1')


@windows_only
def test_startup_folder_persistence_is_denied(ws):
    target = (pathlib.Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
              / "Programs" / "Startup" / f"pwn_{uuid.uuid4().hex}.bat")
    sh(ws, f'cmd /c echo x> "{target}"')
    assert not target.exists()


@windows_only
@pytest.mark.skipif(not CURL, reason="curl.exe is not on this Windows")
def test_no_network_data_path(ws):
    assert "[exit code 0]" not in sh(ws, "curl.exe -sS -m 5 -o NUL https://example.com")
    assert "[exit code 0]" not in sh(ws, "curl.exe -sS -m 5 -o NUL http://93.184.216.34")  # raw IP, no DNS needed
    out = sh(ws, "nslookup example.com 2>&1")
    assert ("timed out" in out) or ("[STOPPED" in out) or ("Addresses:" not in out and "Name:" not in out)


@windows_only
def test_raw_socket_connect_is_forbidden(ws):
    probe = ws / "sock.py"
    probe.write_text(
        "import socket\n"
        "s = socket.socket(); s.settimeout(4)\n"
        "try:\n"
        "    s.connect(('93.184.216.34', 80)); print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', getattr(e, 'winerror', e))\n"
    )
    out = sh(ws, f'{sys.executable} sock.py')
    assert "CONNECTED" not in out


@windows_only
def test_bounded_process_spawn_is_killed_with_no_orphans(ws):
    """A command that fans out into detached children and then outlives its timeout must be killed
    whole by the Job Object — no orphan survives (the process-explosion guard)."""
    psutil = pytest.importorskip("psutil")
    tok = f"STRESSSLEEP_{uuid.uuid4().hex[:8]}"

    def sleepers():
        out = []
        for p in psutil.process_iter(["cmdline"]):
            try:
                if p.info["cmdline"] and tok in " ".join(p.info["cmdline"]):
                    out.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return out

    sh(ws, f"cmd /c (for /L %i in (1,1,8) do start /b ping -n 30 -w 1000 127.0.0.1 {tok}) "
           f"& ping -n 30 -w 1000 127.0.0.1 {tok}", timeout=5)
    time.sleep(2)
    leftover = sleepers()
    for p in leftover:                      # safety net: never leave the test's own spawns behind
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    assert leftover == [], f"{len(leftover)} orphan(s) survived the job kill"
