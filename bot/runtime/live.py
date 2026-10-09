"""The process-wide unit runtime engine and its wiring into ABP (docs/runtime.md, phase 2).

* `get_engine()` builds the engine once (state in the application database, branches next to it), with every
  built-in feature area registered as a unit factory (bot/runtime/legacy.py).
* `selected_units()` decides what boots: ABP_UNITS = "all" (default) | "none" | a comma list, else the config key
  `runtime.units`; the default manifest has every built-in area enabled, so ABP behaves as before.
* `route_fallback()` gives the router's "no route matched" handler to the dispatcher: /api/... paths, and the route
  prefixes of the feature areas (their routes are unit contributions served by the dispatcher, not registered on the
  app). An area that is not active answers 404 `unit_unavailable` (websockets close 4404). Until the engine has
  booted, the areas' routes are served from a plain app built from the loaded modules (an app used without its
  lifespan behaves as it always did).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from bot.runtime import legacy, pluggables
from bot.runtime.engine import RuntimeEngine, UnitFactories
from bot.runtime.manifest import BranchStore, InstanceSpec, Manifest
from bot.runtime.state import StateStore
from bot.runtime.unit import DEFAULT_INSTANCE, Instance, make_ref

logger = logging.getLogger("bot.runtime.live")

_engine: Optional[RuntimeEngine] = None
_key: Any = None
booted = False
_loop: Any = None            # the event loop the engine runs on (set by boot)
_failures: dict[str, str] = {}      # unit id -> why its routes could not be registered (set by build_app)


def record_failure(unit_id: str, error: str) -> None:
    """build_app could not import / register an area's routes: that unit starts failed with this error."""
    _failures[unit_id] = error
    logger.error("routes of unit %s could not be registered: %s", unit_id, error)


def failure_of(unit_id: str) -> str:
    return _failures.get(unit_id, "")


def clear_failures() -> None:
    _failures.clear()


def get_engine() -> RuntimeEngine:
    """The engine for the current data folder (rebuilt if the database or the area table changed, e.g. in tests)."""
    global _engine, _key, booted
    from bot import db

    key = (str(db.DB_PATH), tuple(legacy.AREAS))
    if _engine is None or key != _key:
        if _engine is not None:
            try:
                _engine.store.close()
            except Exception:  # noqa: BLE001
                pass
        factories = UnitFactories()
        legacy.register_factories(factories)
        pluggables.register_factories(factories)        # ext:/plugin:/wasm:/mcp: units (phase 3B)
        data_dir = Path(db.DB_PATH).parent
        _engine = RuntimeEngine(factories=factories, store=StateStore(db.DB_PATH),
                                branches=BranchStore(data_dir / "runtime" / "branches"))
        _engine.version_resolver = pluggables.version_of
        pluggables.reset()
        _key, booted = key, False
    return _engine


def reset() -> None:
    global _engine, _key, booted
    _engine, _key, booted = None, None, False


def selected_units(config: Optional[dict] = None) -> list[str]:
    """Unit ids to run: ABP_UNITS, else config `runtime.units`, else all built-in areas."""
    raw: Any = os.environ.get("ABP_UNITS")
    if raw is None or not str(raw).strip():
        if config is None:
            from bot.config import config as _config
            config = _config.current or {}
        raw = (config.get("runtime") or {}).get("units")
    if raw is None:
        return list(legacy.AREAS)
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip() for x in raw]
    else:
        text = str(raw).strip()
        if text.lower() == "all":
            return list(legacy.AREAS)
        if text.lower() == "none":
            return []
        names = [p.strip() for p in text.split(",")]
    return [n for n in dict.fromkeys(names) if n]


def boot_manifest() -> Manifest:
    instances = {make_ref(u, DEFAULT_INSTANCE): InstanceSpec(u) for u in selected_units()}
    if str(os.environ.get("ABP_UNITS") or "").strip().lower() != "none":
        instances.update(pluggables.boot_instances())        # extensions, plugins, WASM plugins, MCP servers
    return Manifest(instances=instances)


async def boot() -> None:
    """Load the selected units. A unit that fails is reported (GET /api/runtime) and left out; ABP carries on."""
    global booted, _loop
    engine = get_engine()
    _loop = asyncio.get_running_loop()
    try:
        report = await engine.boot(boot_manifest())
        pluggables.note_boot_failures(engine)
        if report.failed:
            logger.warning("units that failed to load: %s", report.failed)
    except Exception as exc:  # noqa: BLE001 - the runtime never stops ABP from starting
        engine.boot_error = f"{type(exc).__name__}: {exc}"
        logger.error("unit runtime boot failed; continuing without units: %s", engine.boot_error, exc_info=True)
    booted = True


async def shutdown() -> None:
    global booted
    booted = False
    if _engine is not None:
        # Stopping the engine unregisters the tools of every active unit; the process-wide registry is what an
        # agent without the dashboard (and the next app in this process) uses, so put those back afterwards.
        active = [r.partition("@")[0] for r in list(_engine._active)]
        try:
            await _engine.shutdown()
            legacy.restore_tools(active)
        except Exception:  # noqa: BLE001
            logger.exception("unit runtime shutdown failed")


def run_sync(coro_factory: Any, timeout: float = 120.0) -> Any:
    """Run an engine transaction from synchronous code. From a worker thread it runs on the engine's loop and waits
    for the result; from the loop's own thread (which must not block) it is scheduled and None is returned; before
    the engine has booted it does nothing (the caller's direct effect is all there is yet)."""
    if not booted or _loop is None or not _loop.is_running():
        return None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        return asyncio.run_coroutine_threadsafe(coro_factory(), _loop).result(timeout)
    if running is _loop:
        get_engine().supervisor.spawn("runtime:sync", coro_factory, "sync")
    return None


def state_of(unit_id: str) -> str:
    """active | loading | failed | unloaded, for the default instance of a unit."""
    engine = get_engine()
    ref = make_ref(unit_id)
    if ref in engine._active:
        return "active"
    return engine.dispatcher.table.states.get(ref, "unloaded")


def route_fallback(original: Any) -> Any:
    """The router's handler for requests no route matched: /api/<unit>... that the dispatcher can serve (a live
    instance, or a unit that is loading / failed) goes to it, as do the route prefixes of the feature areas (a unit
    that is not active answers `unit_unavailable`); everything else keeps the app's own 404."""
    async def handler(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            area = legacy.area_for_path(path)
            if area is not None and not booted:
                await legacy.static_app()(scope, receive, send)
                return
            if area is not None or path.startswith("/api/"):
                engine = get_engine()
                dispatcher = engine.dispatcher
                if not booted:
                    await pluggables.ensure_loaded(engine)       # a dashboard served without the lifespan still has its extensions
                found = dispatcher._resolve(scope)
                if isinstance(found[0], Instance):
                    await dispatcher(scope, receive, send)
                    return
                if area is not None:
                    await dispatcher._unavailable(scope, receive, send, area, state_of(area))
                    return
                if found[2] != "unloaded" or dispatcher.is_known_path(path):
                    await dispatcher(scope, receive, send)
                    return
        await original(scope, receive, send)

    return handler
