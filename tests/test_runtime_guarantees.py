"""The nine guarantees of docs/runtime.md ("Guarantees (each is a test)") against the REAL app: bot.dashboard.server
.build_app() served by a real uvicorn on a free port, with a temporary data folder (tests/runtime_realapp.py). The
areas' background tasks are idle stand-ins; the toy unit (tests/runtime_toys.py) is registered in the live engine for
the guarantees that need a unit with versions, health and state. Nothing here touches the owner's data or ports."""
from __future__ import annotations

import dataclasses
import threading
import time

import httpx
import pytest
from runtime_realapp import H, real_app  # noqa: F401  (real_app is a fixture)

from bot.agent_runtime import toolspec
from bot.runtime import legacy

AREA_IDS = list(legacy.AREAS)


def _tool_names(unit: str) -> list:
    try:
        return legacy.tool_names(legacy.AREAS[unit])
    except Exception:  # noqa: BLE001 - an area whose optional dependency is missing has no tools here
        return []


def _registered(names) -> list:
    return [n for n in names if n in toolspec._registered]


def _probe(app, area) -> dict:
    """The unit_unavailable error object a request under the area's first route prefix gets, or {}."""
    r = app.get(area.routes[0] + "/__probe__")
    try:
        body = r.json()
    except ValueError:
        return {}
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) and err.get("code") == "unit_unavailable" else {}


def _groups(app, unit: str) -> list:
    return [g for g in app.engine().supervisor.groups() if g.startswith(f"{unit}@")]


POOL_THREADS = ("asyncio_", "AnyIO worker", "ThreadPoolExecutor")      # lazily created, reused, bounded: not a leak


def _own_threads(names) -> list:
    return sorted(n for n in names if not n.startswith(POOL_THREADS))


def _until(fn, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return bool(fn())


# ----------------------------------------------------------------------------------------------- G1: no units
def test_g1_no_units_serves_the_dashboard_chat_config_and_runtime(real_app):
    names = {u: _tool_names(u) for u in AREA_IDS}
    for ns in names.values():
        for n in ns:
            toolspec.unregister(n)
    app = real_app("none")
    page = app.get("/")
    assert page.status_code == 200 and "<html" in page.text.lower() and "module-host.js" in page.text
    assert app.get("/api/overview").status_code == 200
    assert app.get("/api/config").status_code == 200
    assert app.get("/api/server-chat/whoami").status_code == 200                # the chat endpoints
    assert app.get("/api/server-chat/conversations").status_code == 200
    status = app.status()
    assert status["units"] == [] and status["failed"] == {} and not status["boot_error"]
    assert app.panels() == []
    assert not any(_registered(ns) for ns in names.values()), "no area tool"
    assert app.engine().supervisor.total() == 0, "no area task"
    for area in legacy.AREAS.values():
        assert _probe(app, area).get("state") == "unloaded", area.id
    assert app.get("/api/nope-nothing").json() == {"detail": "Not Found"}


# ----------------------------------------------------------------------------------------------- G2: every unit
def test_g2_all_units_are_live(real_app):
    app = real_app("all")
    status = app.status()
    assert {u["unit"] for u in status["units"]} == set(AREA_IDS) and status["failed"] == {}
    assert app.builtin_units() == {u for u, a in legacy.AREAS.items() if a.panel}
    for area in legacy.AREAS.values():
        assert not _probe(app, area), area.id
        assert all(n in toolspec._registered for n in _tool_names(area.id)), area.id
    assert _until(lambda: all(_groups(app, u) for u, a in legacy.AREAS.items() if a.tasks))
    assert app.get("/api/overview").status_code == 200


def test_g2_a_failing_area_is_reported_and_the_rest_serve(real_app, monkeypatch, tmp_path):
    (tmp_path / "abp_test_raising_tools.py").write_text("raise RuntimeError('tools exploded')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(legacy.AREAS, "hosting", dataclasses.replace(
        legacy.AREAS["hosting"], tools=("abp_test_raising_tools",)))
    monkeypatch.delitem(legacy.RECORDED, "hosting", raising=False)
    app = real_app("all")
    status = app.status()
    assert list(status["failed"]) == ["hosting@default"] and "tools exploded" in status["failed"]["hosting@default"]
    assert app.units() == set(AREA_IDS) - {"hosting"}
    assert _probe(app, legacy.AREAS["hosting"])["state"] == "failed"
    assert not _probe(app, legacy.AREAS["cluster"])
    assert app.get("/api/overview").status_code == 200
    r = app.load("hosting")                                  # loading it again reports, never raises
    assert r.status_code == 409 and "tools exploded" in str(r.json()["detail"]["failed"])
    assert app.units() == set(AREA_IDS) - {"hosting"}


# ----------------------------------------------------------------------------------------------- G3: no leftovers
@pytest.mark.parametrize("unit", AREA_IDS)
def test_g3_load_use_unload_load_leaves_nothing_behind(real_app, unit):
    area = legacy.AREAS[unit]
    names = _tool_names(unit)
    app = real_app("all")
    others = [a for a in legacy.AREAS.values() if a.id != unit]

    def snapshot():
        """Everything an area contributes, as seen from outside and inside."""
        return {"probe": _probe(app, area).get("state"), "tools": sorted(_registered(names)),
                "panel": unit in app.builtin_units(), "groups": sorted(_groups(app, unit)),
                "active": unit in app.units(), "others": [bool(_probe(app, a)) for a in others]}

    def cycle():
        assert app.unload(unit).json()["ok"]
        unloaded = snapshot()
        counts_unloaded = app.tasks_and_threads()
        assert app.load(unit).json()["ok"]
        _until(lambda: bool(_groups(app, unit)) == bool(area.tasks))
        return unloaded, counts_unloaded, snapshot(), app.tasks_and_threads()

    loaded0 = snapshot()
    assert loaded0["active"] and loaded0["probe"] is None and (loaded0["panel"] == area.panel)
    assert sorted(_registered(names)) == sorted(names)
    _until(lambda: bool(_groups(app, unit)) == bool(area.tasks))
    loaded0 = snapshot()
    assert bool(loaded0["groups"]) == bool(area.tasks)

    # one warm-up cycle (first use may start a lazily created pool thread), then the measured one
    cycle()
    base_tasks, base_threads = app.tasks_and_threads()
    unloaded, (u_tasks, u_threads), loaded, (l_tasks, l_threads) = cycle()

    # unloaded: no route, tool, panel, task of the unit; every other area untouched
    assert unloaded["probe"] == "unloaded"
    assert unloaded["tools"] == [] and not unloaded["panel"] and unloaded["groups"] == [] and not unloaded["active"]
    assert not any(unloaded["others"])
    assert u_tasks <= base_tasks and set(_own_threads(u_threads)) <= set(_own_threads(base_threads)), (
        u_tasks, base_tasks, u_threads)
    # loaded again: exactly what it was before
    for key in ("probe", "tools", "panel", "active", "others"):
        assert loaded[key] == loaded0[key], key
    assert bool(loaded["groups"]) == bool(area.tasks) and len(loaded["groups"]) == len(loaded0["groups"])
    assert _until(lambda: app.tasks_and_threads()[0] == base_tasks, 5.0), (l_tasks, base_tasks)
    assert _own_threads(l_threads) == _own_threads(base_threads), set(l_threads) ^ set(base_threads)


def test_g3_the_unloaded_state_is_identical_after_every_cycle(real_app):
    """Unload every area one after another, then load them all back: the end state equals the start state."""
    app = real_app("all")
    names = {u: _tool_names(u) for u in AREA_IDS}
    before = (app.builtin_units(), sorted(n for ns in names.values() for n in _registered(ns)))
    for unit in AREA_IDS:
        assert app.unload(unit).json()["ok"]
    assert app.units() == set() and app.builtin_units() == set() and app.panels() == []
    assert not any(_registered(ns) for ns in names.values())
    assert _until(lambda: app.engine().supervisor.total() == 0)
    for unit in AREA_IDS:
        assert app.load(unit).json()["ok"]
    assert (app.builtin_units(), sorted(n for ns in names.values() for n in _registered(ns))) == before


# ----------------------------------------------------------------------------------------------- G4: swap under load
def _hammer(app, path: str, workers: int = 6):
    stop = threading.Event()
    results: list = []
    errors: list = []
    versions: set = set()          # which version answered, for units that say so ({"v": ...})

    def work():
        with httpx.Client(base_url=app.url, headers=H, timeout=30.0) as c:
            while not stop.is_set():
                try:
                    r = c.get(path)
                    results.append(r.status_code)
                    try:
                        body = r.json()
                        if isinstance(body, dict) and "v" in body:
                            versions.add(body["v"])
                    except ValueError:
                        pass
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()

    def finish():
        stop.set()
        for t in threads:
            t.join(timeout=30)
        return results, errors

    finish.versions = versions

    return finish


def test_g4_a_toy_swapped_repeatedly_under_load_drops_no_request(real_app):
    app = real_app("none")
    app.toy_units("toy")
    assert app.load("toy", version="1").status_code == 200
    finish = _hammer(app, "/api/toy/x")
    try:
        time.sleep(0.2)
        for v in range(2, 12):
            assert app.swap("toy", str(v)).status_code == 200
            time.sleep(0.03)
    finally:
        results, errors = finish()
    # The requests really ran across the swaps (several versions answered), however slow the machine is.
    assert len(finish.versions) >= 3, sorted(finish.versions)
    assert not errors, errors[:3]
    assert set(results) == {200}, {c: results.count(c) for c in set(results)}
    assert app.get("/api/toy/x").json()["v"] == "11"
    app.settle()
    assert len([g for g in app.engine().supervisor.groups() if g.startswith("toy@")]) == 1, "only the live toy's task"


def test_g4_an_area_swapped_under_load_drops_no_request(real_app):
    app = real_app("vm-harness,ollama")
    finish = _hammer(app, "/api/vm-harness/status", workers=4)
    try:
        time.sleep(0.2)
        for v in range(2, 6):
            r = app.swap("vm-harness", str(v))
            assert r.status_code == 200, r.text
            time.sleep(0.05)
    finally:
        results, errors = finish()
    assert len(results) > 20 and not errors, (len(results), errors[:3])
    assert set(results) == {200}, {c: results.count(c) for c in set(results)}


# ----------------------------------------------------------------------------------------------- G5: failed stage
def test_g5_failing_health_keeps_generation_and_behaviour(real_app):
    app = real_app("none")
    app.toy_units("toy")
    assert app.load("toy", version="1").status_code == 200
    first = app.get("/api/toy/x").json()
    gen = app.generation()
    panels = app.panels()

    bad_swap = app.swap("toy", "2", settings={"health": "failed"})
    assert bad_swap.status_code == 409 and "toy@default" in bad_swap.json()["detail"]["failed"]
    assert app.generation() == gen and app.panels() == panels
    again = app.get("/api/toy/x").json()
    assert again["v"] == "1" and again["n"] == first["n"] + 1

    bad_load = app.load("toy", instance="b", version="1", settings={"health": "failed"})
    assert bad_load.status_code == 409
    assert app.generation() == gen and app.units() == {"toy"}
    assert app.get("/api/toy@b/x").json()["error"]["state"] in ("failed", "unloaded")
    assert app.status()["last_transaction"]["ok"] is False
    # a failed stage left nothing running: a good swap afterwards still works and bumps the generation once
    assert app.swap("toy", "3").status_code == 200 and app.generation() == gen + 1
    assert app.get("/api/toy/x").json()["v"] == "3"


# ----------------------------------------------------------------------------------------------- G6: rollback
def test_g6_rollback_restores_the_previous_behaviour_in_one_transaction(real_app):
    app = real_app("none")
    app.toy_units("toy")
    app.load("toy", version="1")
    app.swap("toy", "2")
    assert app.get("/api/toy/x").json()["v"] == "2"
    gen = app.generation()
    r = app.post("/api/runtime/rollback")
    assert r.status_code == 200 and r.json()["ok"]
    assert app.generation() == gen + 1, "one transaction"
    assert app.get("/api/toy/x").json()["v"] == "1"
    # rolling back an unload brings the unit (and its route) back
    app.unload("toy")
    assert app.get("/api/toy/x").status_code == 404
    assert app.post("/api/runtime/rollback").status_code == 200
    assert app.get("/api/toy/x").json()["v"] == "1"


def test_g6_rollback_of_a_real_area(real_app):
    app = real_app("vm-harness,ollama")
    app.unload("vm-harness")
    assert _probe(app, legacy.AREAS["vm-harness"])
    assert app.post("/api/runtime/rollback").status_code == 200
    assert not _probe(app, legacy.AREAS["vm-harness"]) and app.units() == {"vm-harness", "ollama"}


# ----------------------------------------------------------------------------------------------- G7: instances
def test_g7_two_instances_have_separate_state_and_swap_independently(real_app):
    app = real_app("none")
    app.toy_units("toy")
    assert app.load("toy", version="1").status_code == 200
    assert app.post("/api/runtime/instances", {"unit": "toy", "instance": "b", "version": "1"}).status_code == 200
    for _ in range(3):
        app.get("/api/toy/x")
    assert app.get("/api/toy/x").json() == {"v": "1", "n": 4, "instance": "default"}
    assert app.get("/api/toy@b/x").json() == {"v": "1", "n": 1, "instance": "b"}
    assert app.swap("toy", "2", instance="b").status_code == 200
    after = app.get("/api/toy@b/x").json()
    assert after["v"] == "2" and after["n"] == 2, "its state carried over the swap"
    assert app.get("/api/toy/x").json()["v"] == "1"
    assert app.swap("toy", "3").status_code == 200
    assert app.get("/api/toy/x").json()["v"] == "3" and app.get("/api/toy@b/x").json()["v"] == "2"
    assert app.unload("toy", instance="b").status_code == 200
    assert app.get("/api/toy@b/x").status_code == 404 and app.get("/api/toy/x").status_code == 200
    assert {"toy_toy", "toy_toy@b"} & set(toolspec._registered) == {"toy_toy"}


# ----------------------------------------------------------------------------------------------- G8: branches
def test_g8_a_branch_switch_is_all_or_nothing_and_a_shadow_never_changes_live(real_app):
    app = real_app("none")
    app.toy_units("toy", "other")
    app.load("toy", version="1")
    assert app.post("/api/runtime/branches", {"name": "try", "from": "live"}).status_code == 200
    app.swap("toy", "2", branch="try")
    assert app.load("other", version="1", branch="try").status_code == 200
    gen = app.generation()
    plan = app.get("/api/runtime/plan?target=try").json()
    assert {o["action"] for o in plan["ops"]} == {"swap", "load"}
    assert app.get("/api/toy/x").json()["v"] == "1" and app.generation() == gen, "editing a branch changes nothing"

    # shadow preview: reachable under /api/_branch/try/..., live untouched, its state copy-on-write
    assert app.post("/api/runtime/preview", {"branch": "try"}).status_code == 200
    assert app.get("/api/_branch/try/toy/x").json()["v"] == "2"
    assert app.get("/api/_branch/try/other/x").status_code == 200
    assert app.get("/api/toy/x").json()["v"] == "1" and app.get("/api/other/x").status_code == 404
    live_n = app.get("/api/toy/x").json()["n"]
    assert app.get("/api/_branch/try/toy/x").json()["n"] == 3, "copy-on-write: its own counter"
    assert app.get("/api/toy/x").json()["n"] == live_n + 1, "the shadow's calls never reached live state"
    assert app.generation() == gen and app.units() == {"toy"}
    assert app.http.delete("/api/runtime/preview/try").status_code == 200
    assert app.get("/api/_branch/try/toy/x").status_code == 404

    # a branch with one bad instance: nothing of it is applied
    app.load("other", instance="bad", version="1", settings={"health": "failed"}, branch="try")
    r = app.post("/api/runtime/switch", {"branch": "try"})
    assert r.status_code == 409
    assert app.generation() == gen and app.units() == {"toy"}
    assert app.get("/api/toy/x").json()["v"] == "1" and app.get("/api/other/x").status_code == 404

    # fix the branch and switch: both changes land in one transaction
    assert app.unload("other", instance="bad", branch="try").status_code == 200
    assert app.post("/api/runtime/switch", {"branch": "try"}).status_code == 200
    assert app.generation() == gen + 1
    assert app.get("/api/toy/x").json()["v"] == "2" and app.get("/api/other/x").status_code == 200
    # the old live manifest is the rollback target
    assert app.post("/api/runtime/rollback").status_code == 200
    assert app.get("/api/toy/x").json()["v"] == "1" and app.get("/api/other/x").status_code == 404


def test_a_plugin_install_disable_enable_remove_never_removes_another_units_tools(real_app, tmp_path):
    """A plugin transaction through the dashboard routes touches only the plugin's own tools: after every step the
    tools of every active area are still registered."""
    plugin = tmp_path / "isolation_plugin.py"
    plugin.write_text(
        "def setup(api):\n"
        "    async def handler(tool_input, *, workspace=None, instance_id=None):\n"
        "        return 'ok'\n"
        "    api.register_tool('isolation_plugin_tool', 'Say ok.', {'type': 'object', 'properties': {}}, handler)\n",
        encoding="utf-8")
    app = real_app("all")
    expected = {u: _tool_names(u) for u in AREA_IDS}
    assert any(expected.values())

    def check(step: str) -> None:
        for unit, names in expected.items():
            if unit in app.units():
                assert _registered(names) == names, f"after {step}: {unit} lost {set(names) - set(_registered(names))}"

    check("boot")
    assert app.post("/api/plugins", {"path": str(plugin)}).status_code == 200
    app.settle()
    check("install")
    from bot import plugins
    assert plugins.has_tool("isolation_plugin_tool")
    assert app.post("/api/plugins/isolation_plugin/disable").status_code == 200
    app.settle()
    check("disable")
    assert app.post("/api/plugins/isolation_plugin/enable").status_code == 200
    app.settle()
    check("enable")
    assert app.http.delete("/api/plugins/isolation_plugin").status_code == 200
    app.settle()
    check("remove")
    assert not plugins.has_tool("isolation_plugin_tool")
