"""bot/docker_mgr.py + bot/vm_mgr.py + /api/docker, /api/vms: validated argv
(no injection), a dead Docker daemon is a reportable state not a hang, QEMU
VMs really start/pause/snapshot/stop through QMP (skipped without QEMU), and
every route is desktop-token-only."""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from bot import db, docker_mgr as dk, envfile, vm_mgr as vm
from bot.dashboard.server import build_app


@pytest.fixture
def dcalls(monkeypatch):
    seen = []
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "_run", lambda args, **kw: (seen.append(args) or (True, "ok")))
    return seen


def test_container_create_builds_a_validated_run(dcalls):
    dk.container_create(image="nginx:stable", name="web", ports=["8080:80"], env=["A=b c"],
                        volumes=["data:/data"], restart="unless-stopped", memory="256m", cap_add=["NET_ADMIN"])
    args = dcalls[0]
    assert args[:3] == ["run", "-d", "--name"] and args[-1] == "nginx:stable"
    assert "-p" in args and "8080:80" in args and "--memory" in args


@pytest.mark.parametrize("kw", [
    {"image": "--privileged"}, {"image": "x", "name": "a b"}, {"image": "x", "ports": ["80; rm"]},
    {"image": "x", "env": ["1BAD=x"]}, {"image": "x", "restart": "sometimes"}, {"image": "x", "cap_add": ["net;admin"]},
    {"image": "x", "devices": ["/etc/passwd"]}, {"image": "x", "memory": "lots"},
])
def test_bad_container_options_never_reach_docker(dcalls, kw):
    with pytest.raises(dk.DockerError):
        dk.container_create(**kw)
    assert dcalls == []


def test_exec_is_argv_not_shell_and_ids_cannot_be_flags(dcalls):
    dk.container_exec("web", ["ls", "-la", "; rm -rf /"])
    assert dcalls[0] == ["exec", "web", "ls", "-la", "; rm -rf /"]      # one argv element each, never a shell
    for bad in ("--privileged", "a b", "-v"):
        with pytest.raises(dk.DockerError):
            dk.container_action(bad, "start")


def test_registry_password_goes_over_stdin_never_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "_run", lambda args, stdin=None, **kw: seen.update(args=args, stdin=stdin) or (True, "Login Succeeded"))
    dk.registry_login("ghcr.io", "me", "hunter2-secret")
    assert "hunter2-secret" not in " ".join(seen["args"]) and seen["stdin"] == "hunter2-secret"


def test_a_dead_daemon_is_a_status_not_an_exception(monkeypatch):
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "_run", lambda *a, **k: (False, "docker info timed out after 12s"))
    assert dk.info() == {"installed": True, "running": False, "error": "docker info timed out after 12s"}


def test_stack_deploy_stores_the_compose_file(tmp_path, monkeypatch, dcalls):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    dk.stack_deploy("web", "services:\n  a:\n    image: nginx\n", {"K": "v"})
    assert (tmp_path / "data" / "stacks" / "web" / "compose.yaml").is_file()
    assert dk.stack_get("web")["env_keys"] == ["K"]
    with pytest.raises(dk.DockerError):
        dk.stack_deploy("../evil", "services: {}")


def test_a_hung_docker_call_times_out_and_its_tree_is_killed(monkeypatch, tmp_path):
    fake = tmp_path / ("docker.cmd" if os.name == "nt" else "docker")
    fake.write_text("@echo off\r\nping -n 30 127.0.0.1 >nul\r\n" if os.name == "nt" else "#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    t0 = time.time()
    ok, out = dk._run(["ps"], timeout=2)
    assert not ok and "timed out" in out and time.time() - t0 < 15


# ------------------------------------------------------------------- VMs
_QEMU = vm._exe("qemu-system-x86_64") and vm._exe("qemu-img")


def test_vm_definition_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    for kw in ({"name": "a b"}, {"name": "ok", "arch": "z80"}, {"name": "ok", "cpus": 0},
               {"name": "ok", "memory": "lots"}, {"name": "ok", "display": "rdp"},
               {"name": "ok", "nics": [{"hostfwd": ["tcp::1-:2;calc"]}]},
               {"name": "ok", "disks": [{"path": "a,file=/etc/passwd"}]}):
        with pytest.raises(vm.VMError):
            vm.qemu_define(**kw)
    assert vm.qemu_define("ok", display="none")["machine"] == "q35"
    with pytest.raises(vm.VMError):
        vm.qemu_define("ok")                     # already exists


@pytest.mark.skipif(not _QEMU, reason="QEMU is not installed")
def test_a_real_qemu_vm_runs_pauses_snapshots_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    img = str(tmp_path / "d.qcow2")
    vm.disk_create(img, "32M")
    vm.disk_resize(img, "48M")
    assert vm.disk_info(img)["virtual-size"] == 48 * 1024 * 1024
    vm.qemu_define("t", disks=[{"path": img}], display="none", accel="tcg", memory="64M")
    try:
        assert vm.qemu_start("t")["ok"]
        assert vm.qemu_status("t")["state"] == "running"
        assert vm.qemu_control("t", "pause")["state"] == "paused"
        assert vm.qemu_control("t", "resume")["state"] == "running"
        vm.qemu_snapshot("t", "create", "s1")
        assert "s1" in vm.qemu_snapshot("t", "list")["output"]
        with pytest.raises(vm.VMError):
            vm.qemu_monitor("t", "migrate tcp:1.2.3.4:1")
        assert [x["state"] for x in vm.qemu_list()] == ["running"]
    finally:
        vm.qemu_stop("t", force=True)
    assert vm.qemu_status("t")["running"] is False
    vm.qemu_delete("t")


def test_routes_are_desktop_token_only(temp_db, monkeypatch, dcalls):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    client = TestClient(build_app())
    _, phone = db.create_api_key("phone", kind="device")
    _, peer = db.create_api_key("peer: x", kind="peer_server")
    urls = ["/api/docker/containers", "/api/docker/info", "/api/vms", "/api/vms/backends"]
    for url in urls:
        assert client.get(url).status_code in (401, 403)
        for key in (phone, peer):
            assert client.get(url, headers={"X-Dashboard-Token": key}).status_code in (401, 403)
    assert client.get("/api/docker/templates", headers={"X-Dashboard-Token": "test-token"}).status_code == 200
    assert client.post("/api/docker/containers/--x/action", json={"action": "start"},
                       headers={"X-Dashboard-Token": "test-token"}).status_code == 400
