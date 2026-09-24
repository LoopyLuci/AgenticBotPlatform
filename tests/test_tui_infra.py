"""TUI screens for Containers, VMs, Tailscale and Infra automation, driven with Textual's run_test() against the real
dashboard app (in-process ASGI), with the docker / tailscale command layers faked underneath."""
from __future__ import annotations

import asyncio
import shutil

import httpx
import pytest

from bot import docker_mgr as dk, tailscale_mgr as ts, vm_mgr as vm
from bot.config import config
from bot.dashboard.server import build_app
from bot.tui.app import AgenticBotPlatformTUI
from bot.tui.client import DashboardClient


@pytest.fixture
def client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    temp_path = tmp_path / "backends.yaml"
    shutil.copy(config.path, temp_path)
    monkeypatch.setattr(config, "path", temp_path)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    return DashboardClient("http://testserver", "test-token", transport=httpx.ASGITransport(app=build_app()))


async def _until(pilot, predicate, timeout=10.0):
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        await pilot.pause(0.1)
        waited += 0.1
    assert predicate()


def _run(client, screen_factory, body):
    async def go():
        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(150, 60)) as pilot:
            app.client = client
            await app.push_screen(screen_factory())
            await body(app, app.screen, pilot)
    asyncio.run(go())


def test_containers_screen_lists_and_acts_on_a_container(client, monkeypatch):
    from textual.widgets import Button, DataTable, Label

    from bot.tui.screens.infra import ContainersScreen
    acted = []
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "info", lambda: {"installed": True, "running": True})
    monkeypatch.setattr(dk, "containers", lambda all_=True: [
        {"Names": "web", "Image": "nginx", "State": "exited", "Status": "Exited (0)", "Ports": ""}])
    monkeypatch.setattr(dk, "container_action", lambda c, a: acted.append((c, a)) or {"output": "ok"})
    monkeypatch.setattr(dk, "container_logs", lambda c, tail=200, since=None, timestamps=False: {"logs": "line one"})

    async def body(app, screen, pilot):
        table = screen.query_one("#ct-table", DataTable)
        await _until(pilot, lambda: table.row_count == 1)
        table.move_cursor(row=0)
        await pilot.pause()
        screen.query_one("#ct-start", Button).press()
        await _until(pilot, lambda: acted == [("web", "start")])
        screen.query_one("#ct-logs", Button).press()
        await _until(pilot, lambda: "line one" in "".join(str(x) for x in screen.query_one("#ct-out").lines))
        screen.query_one("#ct-shell", Button).press()          # no docker binary in the test -> reported, never crashes
        await pilot.pause(0.3)

    _run(client, ContainersScreen, body)


def test_containers_screen_says_so_when_docker_is_down(client, monkeypatch):
    from textual.widgets import DataTable, Label

    from bot.tui.screens.infra import ContainersScreen
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "info", lambda: {"installed": True, "running": False, "error": "timed out after 12s"})

    async def body(app, screen, pilot):
        await _until(pilot, lambda: "not responding" in str(screen.query_one("#ct-table", DataTable).get_row_at(0)))
        assert "Start Docker Desktop" in str(screen.query_one("#ct-status", Label).content)

    _run(client, ContainersScreen, body)


def test_vms_screen_runs_a_qemu_monitor_command_on_a_real_vm(client, monkeypatch, tmp_path):
    from bot import envfile
    from textual.widgets import Button, DataTable, Input

    from bot.tui.screens.infra import VmsScreen
    if not (vm._exe("qemu-system-x86_64") and vm._exe("qemu-img")):
        pytest.skip("QEMU is not installed")
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(vm, "hv_list", lambda: [])
    vm.qemu_define("tuivm", display="none", accel="tcg", memory="64M")
    vm.qemu_start("tuivm")

    async def body(app, screen, pilot):
        table = screen.query_one("#vm-table", DataTable)
        await _until(pilot, lambda: table.row_count == 1)
        table.move_cursor(row=0)
        await pilot.pause()
        screen.query_one("#vm-mon", Input).value = "info status"
        screen.query_one("#vm-monitor", Button).press()
        await _until(pilot, lambda: "running" in "".join(str(x) for x in screen.query_one("#vm-out").lines))
        screen.query_one("#vm-force", Button).press()
        await _until(pilot, lambda: vm.qemu_status("tuivm")["running"] is False)

    try:
        _run(client, VmsScreen, body)
    finally:
        vm.qemu_stop("tuivm", force=True)
        vm.qemu_delete("tuivm")


def test_tailscale_screen_toggles_a_preference_and_publishes(client, monkeypatch):
    from textual.widgets import Button, DataTable, Input

    from bot.tui.screens.infra import TailscaleScreen
    calls = []
    monkeypatch.setattr(ts, "is_installed", lambda: True)
    monkeypatch.setattr(ts, "status", lambda: {"Peer": {"a": {"HostName": "laptop", "TailscaleIPs": ["100.1.1.1"], "OS": "linux", "Online": True}}})
    monkeypatch.setattr(ts, "prefs", lambda: {"ShieldsUp": False, "CorpDNS": True, "RouteAll": False, "RunSSH": False,
                                              "AutoUpdate": {"Apply": True, "Check": True}, "AdvertiseRoutes": []})
    monkeypatch.setattr(ts, "_run", lambda args, timeout=30.0, stdin=None: (calls.append(args) or (True, "ok")))

    async def body(app, screen, pilot):
        from textual.widgets import Select
        table = screen.query_one("#ts-table", DataTable)
        await _until(pilot, lambda: table.row_count == 1)                      # peers view
        screen.query_one("#ts-view", Select).value = "prefs"
        await _until(pilot, lambda: table.row_count >= 8)
        table.move_cursor(row=list(table.rows).index(next(k for k in table.rows if k.value == "shields_up")))
        await pilot.pause()
        screen.query_one("#ts-toggle", Button).press()
        await _until(pilot, lambda: ["set", "--shields-up=true"] in calls)
        screen.query_one("#ts-target", Input).value = "3000"
        screen.query_one("#ts-publish", Button).press()
        await _until(pilot, lambda: any(c[:1] == ["serve"] and "3000" in c for c in calls))

    _run(client, TailscaleScreen, body)


def test_rules_screen_creates_toggles_runs_and_deletes(client, monkeypatch):
    from textual.widgets import Button, DataTable, Input

    from bot.tui.screens.infra import RulesScreen
    monkeypatch.setattr(dk, "container_action", lambda c, a: {"output": "restarted"})
    monkeypatch.setattr(dk, "containers", lambda all_=True: [])

    async def body(app, screen, pilot):
        table = screen.query_one("#ir-table", DataTable)
        screen.query_one("#ir-target", Input).value = "web"
        screen.query_one("#ir-create", Button).press()
        await _until(pilot, lambda: table.row_count == 1)
        table.move_cursor(row=0)
        await pilot.pause()
        screen.query_one("#ir-toggle", Button).press()
        await _until(pilot, lambda: table.get_row_at(0)[1] == "no")
        screen.query_one("#ir-run", Button).press()
        await _until(pilot, lambda: "restarted" in "".join(str(x) for x in screen.query_one("#ir-out").lines))
        screen.query_one("#ir-delete", Button).press()
        await _until(pilot, lambda: table.row_count == 0)

    _run(client, RulesScreen, body)


def test_bot_list_has_a_key_for_each_new_screen():
    from bot.tui.screens.bot_list import BotListScreen
    keys = {b[0]: b[1] for b in BotListScreen.BINDINGS}
    assert keys["t"] == "tailscale" and keys["d"] == "containers" and keys["v"] == "vms" and keys["o"] == "automation"
