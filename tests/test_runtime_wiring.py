"""The unit runtime wired into the real dashboard app (bot/runtime/live.py, legacy.py): boot selection, route gating,
tool registration, load/unload over HTTP, and failure containment. Uses the real build_app() and a temp database."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bot.agent_runtime import toolspec
from bot.dashboard.server import build_app
from bot.runtime import legacy, live

TOKEN = "test-token"
H = {"X-Dashboard-Token": TOKEN}
PILOTS = ("vm-harness", "hermes-manager", "ollama")
ROUTES = {"vm-harness": "/api/vm-harness/status", "hermes-manager": "/api/hermes-manager/status",
          "ollama": "/api/ollama/status"}


def _tool_names(unit: str) -> list[str]:
    return legacy.tool_names(legacy.AREAS[unit])


@pytest.fixture
def pilot_tools():
    """Tool names of the pilot areas; the toolspec registry is process-wide, so start with them unregistered and
    put back what an earlier test had registered."""
    names = {u: _tool_names(u) for u in PILOTS}
    before = {n: toolspec._registered.get(n) for ns in names.values() for n in ns}
    for ns in names.values():
        for n in ns:
            toolspec.unregister(n)
    yield names
    for ns in names.values():
        for n in ns:
            toolspec.unregister(n)
    for n, entry in before.items():
        if entry is not None:
            toolspec._registered[n] = entry


@pytest.fixture
def env(monkeypatch, temp_db, pilot_tools):
    from bot.hermes_manager import harness as hm
    from bot.ollama import harness as ol
    from bot.vm_harness import harness as vm

    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    monkeypatch.delenv("ABP_UNITS", raising=False)
    monkeypatch.setattr(vm, "status", lambda backends=True: {"unit": "vm"})
    monkeypatch.setattr(hm, "status", lambda: {"unit": "hm"})
    monkeypatch.setattr(ol, "status", lambda: {})
    monkeypatch.setattr(ol, "summary", lambda full=None: {"unit": "ol"})
    live.reset()
    yield pilot_tools
    live.reset()


def _manifest_units(c: TestClient) -> set[str]:
    return {p["unit"] for p in c.get("/api/ui/manifest", headers=H).json()["panels"] if p["kind"] == "builtin"}


def test_all_units_listed_and_pilot_routes_work(env):
    with TestClient(build_app()) as c:
        units = _manifest_units(c)
        assert set(PILOTS) <= units and units == set(legacy.AREAS)       # every page stays visible
        for unit, path in ROUTES.items():
            assert c.get(path, headers=H).status_code == 200, unit
        for names in env.values():
            assert names and all(toolspec.has_handler(n) or n in toolspec._registered for n in names)
        status = c.get("/api/runtime", headers=H).json()
        assert {u["unit"] for u in status["units"]} == set(legacy.AREAS) and not status["failed"]
        # unmatched paths keep the app's own 404 and wrong methods keep 405
        assert c.get("/api/nope-nothing", headers=H).json() == {"detail": "Not Found"}
        assert c.delete("/api/vm-harness/status", headers=H).status_code == 405


def test_none_boots_core_only(env, monkeypatch):
    monkeypatch.setenv("ABP_UNITS", "none")
    with TestClient(build_app()) as c:
        assert c.get("/api/overview", headers=H).status_code == 200
        assert c.get("/api/config", headers=H).status_code == 200
        assert _manifest_units(c) == set()
        for unit, path in ROUTES.items():
            r = c.get(path, headers=H)
            assert r.status_code == 404 and r.json()["error"] == {
                "code": "unit_unavailable", "unit": unit, "state": "unloaded"}, unit
        for names in env.values():
            assert not any(n in toolspec._registered for n in names)


def test_comma_list_selects_units(env, monkeypatch):
    monkeypatch.setenv("ABP_UNITS", "ollama, vm-harness")
    with TestClient(build_app()) as c:
        assert _manifest_units(c) == {"ollama", "vm-harness"}
        assert c.get(ROUTES["hermes-manager"], headers=H).status_code == 404
        assert c.get(ROUTES["ollama"], headers=H).status_code == 200


def test_config_key_selects_units_when_env_unset():
    assert live.selected_units({"runtime": {"units": ["ollama"]}}) == ["ollama"]
    assert live.selected_units({"runtime": {"units": "none"}}) == []
    assert live.selected_units({}) == list(legacy.AREAS)


def test_unload_and_load_a_unit_over_http(env):
    with TestClient(build_app()) as c:
        names = env["vm-harness"]
        assert c.get(ROUTES["vm-harness"], headers=H).status_code == 200
        assert all(n in toolspec._registered for n in names)
        gen = c.get("/api/runtime", headers=H).json()["generation"]
        r = c.post("/api/runtime/units/vm-harness/unload", headers=H)
        assert r.status_code == 200 and r.json()["ok"]
        assert not any(n in toolspec._registered for n in names)
        assert c.get(ROUTES["vm-harness"], headers=H).status_code == 404
        assert c.get(ROUTES["vm-harness"], headers=H).json()["error"]["code"] == "unit_unavailable"
        assert c.get(ROUTES["ollama"], headers=H).status_code == 200            # others untouched
        assert "vm-harness" not in _manifest_units(c)
        events = c.get(f"/api/runtime/events?since={gen}", headers=H).json()["events"]
        assert events and events[-1]["removed"] == ["vm-harness@default"]
        r = c.post("/api/runtime/units/vm-harness/load", headers=H)
        assert r.status_code == 200
        assert c.get(ROUTES["vm-harness"], headers=H).status_code == 200
        assert all(n in toolspec._registered for n in names)
        assert "vm-harness" in _manifest_units(c)
        assert c.post("/api/runtime/units/vm-harness/unload").status_code == 401


def test_runtime_changed_is_broadcast_to_the_websocket_hook(env, monkeypatch):
    from bot.dashboard import server

    seen: list = []
    monkeypatch.setattr(server, "_broadcast_soon", lambda payload: seen.append(payload))
    with TestClient(build_app()) as c:
        seen.clear()
        c.post("/api/runtime/units/ollama/unload", headers=H)
    assert any(e.get("type") == "runtime.changed" and e["removed"] == ["ollama@default"] for e in seen)


def test_a_unit_whose_import_raises_fails_alone(env, monkeypatch):
    broken = legacy.Area("broken", "Broken", tools=("bot._no_such_module_for_test",), converted=False)
    monkeypatch.setitem(legacy.AREAS, "broken", broken)
    monkeypatch.setenv("ABP_UNITS", "broken,vm-harness,ollama")
    with TestClient(build_app()) as c:
        status = c.get("/api/runtime", headers=H).json()
        assert "broken@default" in status["failed"] and "ModuleNotFoundError" in status["failed"]["broken@default"]
        assert {u["unit"] for u in status["units"]} == {"vm-harness", "ollama"}
        assert c.get(ROUTES["vm-harness"], headers=H).status_code == 200
        assert "broken" not in _manifest_units(c)
        r = c.post("/api/runtime/units/broken/load", headers=H)          # loading it again reports, never raises
        assert r.status_code == 409 and r.json()["detail"]["failed"]
        assert c.get("/api/overview", headers=H).status_code == 200


def test_task_entries_run_and_stop_with_the_unit(env, monkeypatch):
    import sys
    import types

    ran = {"started": 0, "stopped": 0}

    async def run_forever(stop_event):
        import asyncio
        ran["started"] += 1
        try:
            await asyncio.Event().wait()
        finally:
            ran["stopped"] += 1

    mod = types.ModuleType("abp_test_tasks_mod")
    mod.run_forever = run_forever
    monkeypatch.setitem(sys.modules, "abp_test_tasks_mod", mod)
    monkeypatch.setitem(legacy.AREAS, "tasked", legacy.Area("tasked", "Tasked", tasks=("abp_test_tasks_mod:run_forever",)))
    monkeypatch.setenv("ABP_UNITS", "tasked")
    with TestClient(build_app()) as c:
        import time
        for _ in range(50):
            if ran["started"]:
                break
            time.sleep(0.02)
        assert ran["started"] == 1
        assert c.post("/api/runtime/units/tasked/unload", headers=H).status_code == 200
        for _ in range(100):
            if ran["stopped"]:
                break
            time.sleep(0.02)
        assert ran["stopped"] == 1


def _tools_in_fresh_process(units: str) -> list[str]:
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = ("import json; from bot.agent_runtime import tools, toolspec; "
            "print(json.dumps(sorted(toolspec._registered)))")
    env = {**os.environ, "ABP_UNITS": units}
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_tools_register_on_import_without_the_dashboard():
    expected = {n for u in PILOTS for n in _tool_names(u)}
    assert expected
    assert expected <= set(_tools_in_fresh_process("all"))
    assert not expected & set(_tools_in_fresh_process("none"))
    only = set(_tools_in_fresh_process("ollama"))
    assert set(_tool_names("ollama")) <= only and not set(_tool_names("vm-harness")) & only


def test_a_boot_failure_is_recorded_and_shown(env, monkeypatch):
    async def boom(self, manifest=None):
        raise RuntimeError("kaput")

    from bot.runtime.engine import RuntimeEngine
    monkeypatch.setattr(RuntimeEngine, "boot", boom)
    with TestClient(build_app()) as c:
        assert "kaput" in c.get("/api/runtime", headers=H).json()["boot_error"]
        assert c.get("/api/overview", headers=H).status_code == 200
