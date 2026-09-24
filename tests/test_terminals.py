"""Interactive terminals (bot/terminal_broker.py + /api/terminals/ws): real PTY and QEMU sessions, allow-listed
argv, strict token auth. The container shell is exercised through a stand-in `docker` (Docker itself may be down)."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bot import docker_mgr as dk, envfile, terminal_broker as tb, vm_mgr as vm
from bot.dashboard.server import build_app

WIN = os.name == "nt"


def _fake_docker(tmp_path, monkeypatch):
    """A `docker` that ignores its arguments and runs a tiny interactive echo program."""
    prog = tmp_path / "echo_shell.py"
    prog.write_text("import sys\nprint('READY', flush=True)\n"
                    "for line in sys.stdin:\n    line=line.strip()\n    print('got:'+line, flush=True)\n    if line=='exit': break\n",
                    encoding="utf-8")
    if WIN:
        exe = tmp_path / "docker.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{prog}"\r\n')
    else:
        exe = tmp_path / "docker"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{prog}"\n')
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


def _collect(ws, needle, secs=15):
    seen, end = "", time.time() + secs
    while time.time() < end and needle not in seen:
        msg = ws.receive_json()
        if msg["type"] == "output":
            seen += msg["data"]
        elif msg["type"] in ("exit", "error"):
            break
    return seen


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_container_argv_is_built_only_from_validated_pieces(monkeypatch):
    monkeypatch.setattr(tb.shutil, "which", lambda n: "/bin/" + n)
    assert tb.container_argv("web", "bash", "root") == ["/bin/docker", "exec", "-it", "-e", "TERM=xterm-256color",
                                                       "-u", "root", "web", "bash"]
    assert tb.container_argv("web")[-3:-1] == ["sh", "-c"]
    for bad in ({"container": "--privileged"}, {"container": "a b"}, {"container": "web", "shell": "rm -rf /"},
                {"container": "web", "user": "--x"}):
        with pytest.raises((tb.TerminalError, dk.DockerError)):
            tb.container_argv(**bad)


@pytest.mark.skipif(WIN and not __import__("importlib").util.find_spec("winpty"), reason="pywinpty missing")
def test_a_container_shell_works_end_to_end_over_the_websocket(client, tmp_path, monkeypatch):
    _fake_docker(tmp_path, monkeypatch)
    with client.websocket_connect("/api/terminals/ws?kind=container&target=web&token=test-token") as ws:
        assert ws.receive_json()["type"] == "ready"
        assert "READY" in _collect(ws, "READY")
        ws.send_json({"type": "input", "data": "hello world\r"})
        assert "got:hello world" in _collect(ws, "got:hello world")
        ws.send_json({"type": "resize", "cols": 120, "rows": 40})
        ws.send_json({"type": "input", "data": "exit\r"})
    assert tb.list_sessions() == []          # the session is torn down when the socket closes


def test_the_terminal_socket_is_desktop_token_only(client, tmp_path, monkeypatch):
    from bot import db
    _fake_docker(tmp_path, monkeypatch)
    _, phone = db.create_api_key("phone", kind="device")
    _, peer = db.create_api_key("peer: x", kind="peer_server")
    for bad in ("", "wrong", phone, peer):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/terminals/ws?kind=container&target=web&token={bad}") as ws:
                ws.receive_json()


def test_bad_kinds_and_names_are_reported_not_run(client):
    for q in ("kind=shell&target=x", "kind=container&target=--privileged", "kind=container&target=web&shell=zz"):
        with client.websocket_connect(f"/api/terminals/ws?{q}&token=test-token") as ws:
            assert ws.receive_json()["type"] == "error"


_QEMU = vm._exe("qemu-system-x86_64") and vm._exe("qemu-img")


@pytest.mark.skipif(not _QEMU, reason="QEMU is not installed")
def test_a_real_qemu_vm_serial_console_and_monitor(client, tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    vm.qemu_define("t", display="none", accel="tcg", memory="64M")
    try:
        with pytest.raises(tb.TerminalError):
            tb.open_session("vm-monitor", "t")          # not running yet
        vm.qemu_start("t")
        with client.websocket_connect("/api/terminals/ws?kind=vm-monitor&target=t&token=test-token") as ws:
            assert ws.receive_json()["type"] == "ready"
            assert "(qemu)" in _collect(ws, "(qemu)")
            ws.send_json({"type": "input", "data": "info status\r"})
            assert "running" in _collect(ws, "running")
            ws.send_json({"type": "input", "data": "migrate tcp:1.2.3.4:1\r"})
            assert "isn't allowed" in _collect(ws, "isn't allowed")
        serial = tb.open_session("vm-serial", "t")       # QEMU accepted our connection on its serial socket
        assert serial.alive()
        serial.write("\r")
        tb.close_session(serial)
    finally:
        vm.qemu_stop("t", force=True)
        vm.qemu_delete("t")
