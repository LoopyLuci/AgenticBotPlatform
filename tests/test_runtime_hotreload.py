"""Hot reload through the runtime engine (bot/runtime/reload.py), snapshots of the runtime's manifest, and the router's
tool registration. Toy feature areas live in a temporary package; nothing under bot/ is edited."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bot import hotreload, providers, snapshots
from bot.agent_runtime import toolspec
from bot.config import ConfigManager
from bot.runtime import legacy, live
from bot.runtime.manifest import InstanceSpec, Manifest
from bot.runtime.unit import make_ref

REPO = Path(__file__).resolve().parent.parent


def _tool_src(value: str, name: str) -> str:
    return textwrap.dedent(f"""
        from bot.agent_runtime import toolspec

        VALUE = {value!r}

        async def _handler(tool_input, **_ctx):
            return VALUE

        toolspec.register(
            {{"name": {name!r}, "description": "toy", "input_schema": {{"type": "object", "properties": {{}}}}}},
            toolspec.ToolSpec({name!r}, "read", read_only=True, concurrency_safe=True, origin="registered"), _handler)
    """)


@pytest.fixture
def toys(tmp_path, monkeypatch, temp_db):
    """Three toy areas (toya, toyb, toyc), each a package with one tool (toyc also has a route module, toyc/api.py), on a
    runtime with nothing else loaded."""
    sys.dont_write_bytecode = True
    registry_before = dict(toolspec._registered)        # scrubbing unregisters every area this toy engine isn't running
    for pkg in ("toya", "toyb", "toyc"):
        d = tmp_path / "src" / pkg
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / "tools.py").write_text(_tool_src("one", f"{pkg}_tool"), encoding="utf-8")
    (tmp_path / "src" / "toyc" / "api.py").write_text(_api_src("one"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    importlib.invalidate_caches()
    areas = dict(legacy.AREAS)
    for pkg in ("toya", "toyb"):
        areas[pkg] = legacy.Area(pkg, pkg, order=900, tools=(f"{pkg}.tools",), panel=False)
    areas["toyc"] = legacy.Area("toyc", "toyc", order=900, tools=("toyc.tools",), panel=False,
                                routes=("/api/toyc",), api="toyc.api")
    legacy.set_api_auth("toyc", (), {})
    monkeypatch.setattr(legacy, "AREAS", areas)
    monkeypatch.delenv("ABP_UNITS", raising=False)
    live.reset()
    live.clear_failures()
    engine = live.get_engine()
    monkeypatch.setattr(live, "booted", True)
    monkeypatch.setattr(live, "_engine", engine)
    hotreload._degraded = None
    hotreload._last_events.clear()
    hotreload._last_units.clear()
    yield tmp_path / "src"
    for name in [n for n in sys.modules if n.split(".")[0] in ("toya", "toyb", "toyc")]:
        del sys.modules[name]
    legacy.API_AUTH.pop("toyc", None)
    toolspec._registered.clear()
    toolspec._registered.update(registry_before)
    for table in (legacy.BOOT, legacy.RECORDED, legacy.KNOWN_TOOLS):
        for uid in ("toya", "toyb", "toyc"):
            table.pop(uid, None)
    live.reset()


def _api_src(value: str) -> str:
    return textwrap.dedent(f"""
        VALUE = {value!r}

        def register(app, *auth):
            @app.get("/api/toyc/hello")
            def hello():
                return {{"value": VALUE}}
    """)


def _edit(root: Path, pkg: str, value: str) -> Path:
    path = root / pkg / "tools.py"
    path.write_text(_tool_src(value, f"{pkg}_tool"), encoding="utf-8")
    importlib.invalidate_caches()
    return path


async def _call(name: str) -> str:
    return await toolspec._registered[name][2]({})


async def _load(engine, *units: str):
    manifest = Manifest(instances={make_ref(u): InstanceSpec(u) for u in units})
    report = await engine.apply(manifest, label="test boot")
    assert report.ok, report.error
    return report


def _reload(*paths: Path) -> dict:
    return hotreload.run_cycle(list(paths), unit_reload=hotreload._reload_units, denylist=frozenset(),
                               reload_order=(), platform_modules={})


def test_edit_swaps_the_unit_through_the_engine(toys):
    async def go():
        engine = live.get_engine()
        await _load(engine, "toya")
        assert await _call("toya_tool") == "one"
        generation = engine.generation
        path = _edit(toys, "toya", "two")
        result = await _reload(path)
        assert result["status"] == "applied", result
        assert result["units"][0]["action"] == "swap" and result["units"][0]["ok"]
        assert engine.generation == generation + 1
        assert await _call("toya_tool") == "two"                      # the new behaviour
        active = engine._active[make_ref("toya")]
        assert active.version == legacy.source_version(legacy.AREAS["toya"]) and active.slot == "active"
        await engine.settle()
        old = engine._standby[make_ref("toya")][0]
        assert old.slot == "standby" and old.version == "1"           # drained, kept for rollback
        assert hotreload.status()["units"][0]["unit"] == "toya"
        # the old code is untouched by the swap: a rollback serves it again
        report = await engine.rollback()
        assert report.ok and await _call("toya_tool") == "one"
    asyncio.run(go())


def test_failed_swap_keeps_the_old_version_and_does_not_degrade(toys):
    async def go():
        engine = live.get_engine()
        await _load(engine, "toya")
        path = _edit(toys, "toya", "two")
        assert (await _reload(path))["status"] == "applied"
        generation = engine.generation
        for broken in ("VALUE = (\n", "raise RuntimeError('boom')\n"):    # a syntax error, then an import error
            path.write_text(broken, encoding="utf-8")
            importlib.invalidate_caches()
            result = await _reload(path)
            assert result["status"] == "unit_failed", result
            assert result["units"][0]["ok"] is False
            assert engine.generation == generation                       # nothing committed
            assert await _call("toya_tool") == "two"                     # the old version keeps serving
            last = engine.last_transaction
            assert last is not None and not last.ok and make_ref("toya") in last.failed
            status = hotreload.status()
            assert status["degraded"] is None and status["units"][0]["ok"] is False
            assert status["runtime_last_transaction"]["ok"] is False
            assert sys.modules["toya.tools"].VALUE == "two"               # the live modules are the old ones
        path = _edit(toys, "toya", "three")                              # the next good edit still reloads
        result = await _reload(path)
        assert result["status"] == "applied", result
        assert await _call("toya_tool") == "three" and engine.generation == generation + 1
        assert hotreload.is_degraded() is None
    asyncio.run(go())


def test_unloaded_units_stay_unregistered_across_a_hot_reload(toys):
    async def go():
        engine = live.get_engine()
        await _load(engine, "toya", "toyb")
        assert "toyb_tool" in toolspec._registered
        await engine.unload("toyb")
        assert "toyb_tool" not in toolspec._registered
        # editing the unloaded unit's code, and swapping another unit, must not bring its tools back
        pb = _edit(toys, "toyb", "two")
        result = await _reload(pb)
        assert result["units"][0]["action"] == "dropped"
        assert "toyb_tool" not in toolspec._registered and "toyb.tools" not in sys.modules
        pa = _edit(toys, "toya", "two")
        assert (await _reload(pa))["status"] == "applied"
        assert "toyb_tool" not in toolspec._registered
        assert "toya_tool" in toolspec._registered
        # a registration that slipped in some other way (a module reload's import side effect) is scrubbed
        toolspec.register({"name": "toyb_tool", "description": "x", "input_schema": {"type": "object", "properties": {}}},
                          toolspec.ToolSpec("toyb_tool", "read", origin="registered"), lambda *a, **k: None)
        assert "toyb_tool" in legacy.scrub_inactive_tools() and "toyb_tool" not in toolspec._registered
        # loading it again brings the edited code
        assert (await engine.load("toyb")).ok
        assert await _call("toyb_tool") == "two"
    asyncio.run(go())


def test_kernel_and_unowned_files_keep_the_old_behaviour(toys, tmp_path):
    other = tmp_path / "elsewhere.py"
    other.write_text("X = 1\n", encoding="utf-8")

    async def go():
        result = await hotreload.run_cycle([other], unit_reload=hotreload._reload_units, pkg_root=tmp_path,
                                           pkg_dotted_prefix="zz", denylist=frozenset({"zz.elsewhere"}),
                                           reload_order=(), platform_modules={})
        assert result["status"] == "restart_required" and "units" not in result
    asyncio.run(go())


def test_the_hotreload_endpoints_still_work(toys, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t")
    with TestClient(build_app()) as client:
        body = client.get("/api/hotreload/status", headers={"X-Dashboard-Token": "t"}).json()
    assert "units" in body and body["degraded"] is None


# ------------------------------------------------------------------ snapshots
@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_ROOT", tmp_path / "snapshots")
    backends = tmp_path / "backends.yaml"
    backends.write_text("default_backend: cli\n", encoding="utf-8")
    monkeypatch.setattr("bot.config.config", ConfigManager(path=backends))
    monkeypatch.setattr(snapshots, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(snapshots, "CONFIG_FILES", ("backends.yaml", "providers.yaml"))
    prov = tmp_path / "providers.yaml"
    prov.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=prov))


def test_snapshot_restores_units_versions_and_branches_through_a_transaction(toys, snap_env):
    async def go():
        engine = live.get_engine()
        live._loop = asyncio.get_running_loop()
        await _load(engine, "toya", "toyb")
        engine.fork("experiment")
        snap = snapshots.create_snapshot(label="runtime")
        assert snap["runtime"] is True
        saved = json.loads((snapshots.SNAPSHOTS_ROOT / snap["name"] / "runtime.json").read_text(encoding="utf-8"))
        assert set(saved["live"]["instances"]) == {"toya@default", "toyb@default"}
        assert "experiment" in saved["branches"]
        before = engine.manifest
        # now change things: unload one unit, swap the other to new code, drop the branch
        await engine.unload("toyb")
        assert (await _reload(_edit(toys, "toya", "two")))["status"] == "applied"
        engine.branches.delete("experiment")
        assert engine.manifest != before and await _call("toya_tool") == "two"
        generation = engine.generation
        result = await asyncio.to_thread(snapshots.restore_snapshot, snap["name"])
        assert result["runtime"]["applied"] and result["runtime"]["ok"], result
        assert engine.manifest.id == before.id                              # the earlier units and versions are live
        assert engine.generation > generation
        assert await _call("toya_tool") == "one" and await _call("toyb_tool") == "one"
        assert any(e["generation"] == engine.generation for e in engine.events_since(generation))
        assert engine.last_transaction.label == f"snapshot restore {snap['name']}"
        assert engine.branches.get("experiment") is not None
        assert engine.branches.get("previous") is not None                  # the pre-restore state can be rolled back to
    asyncio.run(go())


def test_snapshot_restore_whose_version_is_gone_changes_nothing(toys, snap_env):
    async def go():
        engine = live.get_engine()
        live._loop = asyncio.get_running_loop()
        await _load(engine, "toya")
        assert (await _reload(_edit(toys, "toya", "two")))["status"] == "applied"
        snap = snapshots.create_snapshot(label="v2")
        assert (await _reload(_edit(toys, "toya", "three")))["status"] == "applied"
        await engine.settle()
        engine._standby.clear()                                              # the rollback window is over
        generation = engine.generation
        result = await asyncio.to_thread(snapshots.restore_snapshot, snap["name"])
        assert result["runtime"]["ok"] is False                              # the old source is no longer on disk
        assert engine.generation == generation and await _call("toya_tool") == "three"
    asyncio.run(go())


# ------------------------------------------------------------------ router tools
def _probe(units: str | None, code: str) -> str:
    env = {**os.environ, "ABP_NO_MODULE_PROVIDERS": "1"}
    env.pop("ABP_UNITS", None)
    if units is not None:
        env["ABP_UNITS"] = units
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_importing_the_router_registers_no_tools_with_no_units():
    code = ("import bot.model_router\nfrom bot.agent_runtime import toolspec\n"
            "print('suggest_model' in toolspec._registered)")
    assert _probe("none", code) == "False"
    both = ("import bot.model_router, bot.agent_runtime.tools\nfrom bot.agent_runtime import toolspec\n"
            "print('suggest_model' in toolspec._registered)")
    assert _probe("none", both) == "False"             # the tool module loads, but no unit asked for the router's tools
    assert _probe(None, both) == "True"                # with every unit, the router unit registers them


def test_an_area_swap_changes_its_http_handlers_and_unload_answers_unit_unavailable(toys):
    import httpx

    async def not_found(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"core 404"})

    async def go():
        engine = live.get_engine()
        await _load(engine, "toyc")
        asgi = live.route_fallback(not_found)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://t") as http:
            assert (await http.get("/api/toyc/hello")).json() == {"value": "one"}
            assert (await http.post("/api/toyc/hello")).status_code == 405
            assert (await http.get("/api/toyc/nothing")).status_code == 404
            path = toys / "toyc" / "api.py"
            path.write_text(_api_src("two"), encoding="utf-8")
            importlib.invalidate_caches()
            result = await _reload(path)
            assert result["status"] == "applied" and result["units"][0]["action"] == "swap", result
            assert (await http.get("/api/toyc/hello")).json() == {"value": "two"}
            report = await engine.apply(Manifest(instances={}), label="unload")
            assert report.ok, report.error
            gone = await http.get("/api/toyc/hello")
            assert gone.status_code == 404
            assert gone.json() == {"error": {"code": "unit_unavailable", "unit": "toyc", "state": "unloaded"}}

    asyncio.run(go())


def test_edit_swaps_every_live_instance_of_the_unit_in_one_transaction(toys):
    async def go():
        engine = live.get_engine()
        await _load(engine, "toya")
        assert (await engine.load("toya", instance="second", settings={"k": 1})).ok
        first, second = make_ref("toya"), make_ref("toya", "second")
        assert {first, second} <= set(engine._active)
        generation = engine.generation
        result = await _reload(_edit(toys, "toya", "two"))
        assert result["status"] == "applied", result
        unit = result["units"][0]
        assert unit["ok"] and unit["instances"] == {first: "swapped", second: "swapped"}
        assert engine.generation == generation + 1                    # one transaction for both instances
        version = legacy.source_version(legacy.AREAS["toya"])
        assert engine._active[first].version == version and engine._active[second].version == version
        assert engine.manifest.instances[second].settings == {"k": 1}  # settings of the other instance survive
        # a broken edit leaves both on the old version
        path = _edit(toys, "toya", "two")
        path.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        importlib.invalidate_caches()
        failed = (await _reload(path))["units"][0]
        assert failed["ok"] is False and set(failed["instances"]) == {first, second}
        assert engine._active[first].version == version and engine._active[second].version == version
        assert engine.generation == generation + 1

    asyncio.run(go())
