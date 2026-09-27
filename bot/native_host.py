"""The ABP native-messaging host (docs/browser-extension/DESIGN.md section 3.2 - the secondary transport).

The browser's own primary path to ABP is a direct loopback WebSocket (bot/browser_bridge.py); this exists only for the
case that fails: ABP is not running yet, or something is blocking the loopback connection, and the extension wants to
ask a small, always-installed helper to check or start it. It is launched by the browser itself (never runs on its own,
and is invisible unless something is talking to it), speaks stdio framed as Chrome/Firefox's native messaging protocol
expects (a 4-byte little-endian length prefix, then that many bytes of UTF-8 JSON, both directions), and does exactly
two things: report whether ABP is reachable on a port, and start it if it is not. It reads and writes no other state,
and it never touches the browser or a page - the actual work happens over the ordinary WebSocket once ABP is up.

Not a Python entry point most people run directly: `scripts/install_native_host.py` registers a wrapper the browser
invokes for you, and `python -m bot.native_host` runs the same loop directly for a manual test.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO, Optional

DEFAULT_PORT = 8787
HELLO_TIMEOUT_S = 1.5
LAUNCH_WAIT_S = 20.0


def read_message(stream: BinaryIO) -> Optional[dict]:
    """One native-messaging frame from `stream`, or None at end of stream (the browser closed the pipe)."""
    raw_len = stream.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    if length == 0 or length > 10 * 1024 * 1024:                # a sanity cap; native messages are always tiny here
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return None


def write_message(stream: BinaryIO, message: dict) -> None:
    body = json.dumps(message).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def abp_reachable(port: int, timeout: float = HELLO_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/browser/hello", timeout=timeout) as r:
            return r.status == 200 and json.loads(r.read()).get("abp") is True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def find_launcher() -> Optional[Path]:
    """Where to start ABP from, if it is not already running. Looks next to this file for the marker
    `scripts/install_native_host.py` writes (`native_host_install.json`, `{"launcher": "<path>"}`) - the installer
    always writes this alongside the manifest it registers, so a host running from that same install always finds it."""
    marker = Path(__file__).resolve().parent.parent / "native_host_install.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        launcher = data.get("launcher")
        if launcher and Path(launcher).exists():
            return Path(launcher)
    except (OSError, ValueError):
        pass
    return None


def launch_abp() -> tuple[bool, str]:
    launcher = find_launcher()
    if launcher is None:
        return False, "no ABP install was registered with this native host (reinstall it after installing/moving ABP)"
    try:
        if launcher.suffix.lower() == ".py":
            subprocess.Popen([sys.executable, str(launcher)], cwd=str(launcher.parent), close_fds=True)
        else:
            subprocess.Popen([str(launcher)], cwd=str(launcher.parent), close_fds=True)
        return True, f"started {launcher}"
    except OSError as exc:
        return False, f"could not start {launcher}: {exc}"


def handle(msg: dict) -> dict:
    op = str(msg.get("op") or "status")
    port = int(msg.get("port") or os.environ.get("DASHBOARD_PORT") or DEFAULT_PORT)
    if op == "status":
        return {"op": "status", "port": port, "running": abp_reachable(port)}
    if op == "launch":
        if abp_reachable(port):
            return {"op": "launch", "port": port, "running": True, "started": False}
        ok, detail = launch_abp()
        if not ok:
            return {"op": "launch", "port": port, "running": False, "started": False, "error": detail}
        deadline = time.time() + LAUNCH_WAIT_S
        while time.time() < deadline:
            if abp_reachable(port):
                return {"op": "launch", "port": port, "running": True, "started": True}
            time.sleep(0.5)
        return {"op": "launch", "port": port, "running": False, "started": True, "error": "ABP did not answer within the wait time"}
    return {"op": op, "error": f"unknown op {op!r}"}


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = read_message(stdin)
        if msg is None:
            return
        try:
            write_message(stdout, handle(msg))
        except Exception as exc:  # noqa: BLE001 - a native host that crashes on one bad message is worse than one that reports the error
            try:
                write_message(stdout, {"error": str(exc)})
            except OSError:
                return


if __name__ == "__main__":
    main()
