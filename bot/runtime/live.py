"""The process-wide unit runtime engine and its wiring into ABP (docs/runtime.md, phase 2).

* `get_engine()` builds the engine once (state in the application database, branches next to it), with every
  built-in feature area registered as a unit factory (bot/runtime/legacy.py).
* `selected_units()` decides what boots: ABP_UNITS = "all" (default) | "none" | a comma list, else the config key
  `runtime.units`; the default manifest has every built-in area enabled, so ABP behaves as before.
* `UnitGate` answers 404 `unit_unavailable` for the route prefixes of a unit that is not active (those routes stay
  registered on the app because FastAPI cannot remove them). Until the engine has booted it lets everything through.
* `route_fallback()` gives the router's "no route matched" handler to the dispatcher, for /api/... paths only.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from bot.runtime import legacy
from bot.runtime.engine import RuntimeEngine, UnitFactories
from bot.runtime.manifest import BranchStore, InstanceSpec, Manifest
from bot.runtime.state import StateStore
from bot.runtime.unit import DEFAULT_INSTANCE, Instance, make_ref

logger = logging.getLogger("bot.runtime.live")

_engine: Optional[RuntimeEngine] = None
_key: Any = None
booted = False


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
        data_dir = Path(db.DB_PATH).parent
        _engine = RuntimeEngine(factories=factories, store=StateStore(db.DB_PATH),
                                branches=BranchStore(data_dir / "runtime" / "branches"))
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
    return Manifest(instances={make_ref(u, DEFAULT_INSTANCE): InstanceSpec(u) for u in selected_units()})


async def boot() -> None:
    """Load the selected units. A unit that fails is reported (GET /api/runtime) and left out; ABP carries on."""
    global booted
    engine = get_engine()
    try:
        report = await engine.boot(boot_manifest())
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
        try:
            await _engine.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("unit runtime shutdown failed")


def state_of(unit_id: str) -> str:
    """active | loading | failed | unloaded, for the default instance of a unit."""
    engine = get_engine()
    ref = make_ref(unit_id)
    if ref in engine._active:
        return "active"
    return engine.dispatcher.table.states.get(ref, "unloaded")


def _prefix_table() -> list[tuple[str, str]]:
    return [(p, a.id) for a in legacy.AREAS.values() if a.converted for p in a.routes]


class UnitGate:
    """ASGI middleware: a converted area's routes answer `unit_unavailable` while its unit is not active."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket") and booted:
            path = scope.get("path", "")
            for prefix, unit in _prefix_table():
                if path == prefix or path.startswith(prefix + "/"):
                    state = state_of(unit)
                    if state != "active" and scope["type"] == "websocket":
                        await receive()
                        await send({"type": "websocket.close", "code": 4404, "reason": "unit_unavailable"})
                        return
                    if state != "active":
                        body = json.dumps({"error": {"code": "unit_unavailable", "unit": unit, "state": state}}).encode()
                        await send({"type": "http.response.start", "status": 404, "headers": [
                            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                        await send({"type": "http.response.body", "body": body})
                        return
                    break
        await self.app(scope, receive, send)


def route_fallback(original: Any) -> Any:
    """The router's handler for requests no route matched: /api/<unit>... that the dispatcher can serve (a live
    instance, or a unit that is loading / failed) goes to it; everything else keeps the app's own 404."""
    async def handler(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket") and scope.get("path", "").startswith("/api/") and booted:
            dispatcher = get_engine().dispatcher
            found = dispatcher._resolve(scope)
            if isinstance(found[0], Instance) or found[2] != "unloaded":
                await dispatcher(scope, receive, send)
                return
        await original(scope, receive, send)

    return handler
