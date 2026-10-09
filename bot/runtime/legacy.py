"""Adapter that turns an existing feature area into a unit (docs/runtime.md, phase 2).

An area is declared as one `Area` entry in `AREAS`: its panel (the `data-unit` / section id of its dashboard page),
the route prefixes its FastAPI routes live under, the modules whose import registers its agent tools and the
`module:function` of its background `run_forever(stop_event)` singletons. The adapter imports all of that lazily,
inside `contributions()`, so an area that is not loaded costs nothing and one whose import fails is reported failed
by the engine without touching ABP or any other unit.

An area's routes are part of its unit's contributions: its `register(app, *auth)` (bot/dashboard/<x>_api.py, unchanged)
is run against a collector at stage time, from the possibly freshly imported module, and the resulting router is served
by the dispatcher at the area's own absolute paths. A swap therefore replaces the HTTP handlers too, and an unloaded
unit answers 404 `unit_unavailable` (websockets close 4404) through the dispatcher (bot/runtime/live.py `route_fallback`).

Every area is converted. `converted=False` (an entry that only lists its page) and `panel=False` (an area without a
dashboard page) remain for entries added later or by tests. Route registration (bot/dashboard/server.py) records an
import/registration failure with `bot.runtime.live.record_failure`; the unit then fails at boot with that error.
"""
from __future__ import annotations

import asyncio
import _imp
import importlib
import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

from bot.runtime.unit import Contributions, Health

logger = logging.getLogger("bot.runtime.legacy")


@dataclass(frozen=True)
class Area:
    id: str                         # unit id == the data-unit value of the area's nav link and section
    title: str
    icon: str = ""
    group: str = "Modules"
    order: int = 100
    section: str = ""               # the dashboard section id (defaults to the unit id)
    routes: tuple = ()              # route prefixes gated while the unit is not active, e.g. "/api/ollama"
    tools: tuple = ()               # modules whose import registers the area's agent tools
    tasks: tuple = ()               # "module:function" -- async function(stop_event), run until cancelled
    converted: bool = True
    panel: bool = True              # False: no dashboard page (nothing in the UI manifest)
    packages: tuple = ()            # dotted packages/modules holding the area's code (default: derived from tools/tasks)
    api: str = ""                   # dotted module whose register(app, *auth) adds the area's routes (bot/dashboard/<x>_api)


def _area(id, title, icon, order, section="", **kw) -> tuple:
    return id, Area(id, title, icon, order=order, section=section or id, **kw)


AREAS: dict[str, Area] = dict([
    # every feature area: its routes, tools and tasks belong to the unit
    _area("vm-harness", "VM-Harness", "🖧", 182, routes=("/api/vm-harness",), tools=("bot.vm_harness.tools",), api="bot.dashboard.vm_harness_api"),
    _area("hermes-manager", "Hermes Manager", "☤", 183, routes=("/api/hermes-manager",),
          tools=("bot.hermes_manager.tools",), api="bot.dashboard.hermes_manager_api"),
    _area("ollama", "Ollama", "🦙", 184, routes=("/api/ollama",), tools=("bot.ollama.tools",), api="bot.dashboard.ollama_api"),
    _area("router", "Model Router", "🧭", 160, routes=("/api/router",), tools=("bot.model_router",), api="bot.dashboard.router_api"),
    _area("unsloth", "Unsloth", "🦥", 161, routes=("/api/unsloth",), tools=("bot.unsloth.tools",), api="bot.dashboard.unsloth_api"),
    _area("cluster", "Cluster", "⧉", 162, routes=("/api/cluster",), tools=("bot.cluster.tools",),
          tasks=("bot.cluster.membership:heartbeat_forever",), api="bot.dashboard.cluster_api"),
    _area("private_project", "PrivateProject", "🐙", 163, routes=("/api/private_project",), api="bot.dashboard.private_project_api"),
    _area("vision", "Vision", "👁", 164, routes=("/api/vision",), tools=("bot.vision.tools",), api="bot.dashboard.vision_api"),
    _area("hosting", "Hosting", "🌐", 165, routes=("/api/hosting",), tools=("bot.hosting.tools",),
          tasks=("bot.hosting.service:run_forever",), api="bot.dashboard.hosting_api"),
    _area("storage", "Storage", "🗄", 166, routes=("/api/fileserver",), tools=("bot.fileserver.tools",),
          tasks=("bot.fileserver.service:run_forever",), api="bot.dashboard.fileserver_api"),
    _area("localai", "Local AI", "🦙", 167, routes=("/api/localai",), tools=("bot.localai.tools",),
          tasks=("bot.localai.service:run_forever",), api="bot.dashboard.localai_api"),
    _area("neurallab", "Neural Lab", "🧠", 168, section="lab", routes=("/api/lab",),
          tasks=("bot.neurallab.service:run_forever",), api="bot.dashboard.localai_api"),
    _area("studio", "Studio", "🎨", 169, routes=("/api/studio", "/studio/v"), api="bot.dashboard.studio_api"),
    _area("modules", "Modules", "▦", 170, routes=("/api/modules",), tools=("bot.modules.tools",), api="bot.dashboard.modules_api"),
    _area("transferdaemon", "TransferDaemon", "⇄", 171, routes=("/api/transferdaemon",),
          tools=("bot.transferdaemon.tools",), api="bot.dashboard.transferdaemon_api"),
    _area("power", "Power", "⏻", 172, routes=("/api/power",), tools=("bot.power_tools",), api="bot.dashboard.power_api"),
    # the memory fabric has no page of its own: routes and the upkeep task only
    _area("memoryfabric", "Memory Fabric", "", 173, routes=("/api/memory",),
          tasks=("bot.memoryfabric.service:run_forever",), panel=False, api="bot.dashboard.memory_api"),
])


# area id -> {tool name: (schema, spec, handler, enabled)} -- what importing the area's tools modules registered
RECORDED: dict[str, dict] = {}
BOOT: dict[str, tuple] = {}           # area id -> (tool entries, modules) of version "1", the code as first loaded
KNOWN_TOOLS: dict[str, set] = {}      # area id -> every tool name it has ever registered (kept after a purge)


def _import_tools(area: Area) -> dict:
    """Import the area's tools modules (they register their tools as an import side effect, like every registering
    module) and record the entries that import added or replaced. Raises if an import fails."""
    from bot.agent_runtime import toolspec

    before = dict(toolspec._registered)
    for name in area.tools:
        already = name in sys.modules
        mod = importlib.import_module(name)
        if hasattr(mod, "register_tools") and (already or not getattr(mod, "REGISTER_ON_IMPORT", True)):
            mod.register_tools()                  # imported earlier (maybe unregistered since), or registers only when asked
    entries = {n: e for n, e in toolspec._registered.items() if before.get(n) is not e}
    if entries:
        RECORDED.setdefault(area.id, {}).update(entries)
        KNOWN_TOOLS.setdefault(area.id, set()).update(entries)
    return RECORDED.get(area.id, {})


def import_area_tools() -> list[str]:
    """Called where bot/agent_runtime/tools.py imports tool modules: import the tools of every selected area, each
    in its own try/except (a broken area is logged and left out, never breaks the import). Returns the failed ids."""
    from bot.runtime import live

    failed: list[str] = []
    for unit in live.selected_units():
        area = AREAS.get(unit)
        if area is None or not area.tools:
            continue
        try:
            _import_tools(area)
        except Exception:  # noqa: BLE001
            failed.append(unit)
            logger.exception("tools of %s could not be imported; that unit starts failed", unit)
    return failed


# ------------------------------------------------------------------ source, versions, fresh imports (hot reload)
def area_packages(area: Area) -> tuple:
    """The dotted names whose code belongs to the area: `packages`, else the package (or top-level module) of each
    tools / tasks module; plus its route module (`api`)."""
    out: list[str] = list(area.packages)
    if not out:
        for name in list(area.tools) + [t.partition(":")[0] for t in area.tasks]:
            parent = name.rpartition(".")[0]
            pkg = parent if parent and parent != "bot" else name
            if pkg not in out:
                out.append(pkg)
    if area.api and area.api not in out:
        out.append(area.api)                     # the route module is part of the area: editing it swaps the unit
    return tuple(out)


def _under(name: str, prefixes: Any) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def source_files(area: Area) -> list:
    """Every .py file of the area's packages, found without importing them."""
    import importlib.util
    from pathlib import Path

    files: list = []
    for pkg in area_packages(area):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        if spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                files.extend(p for p in Path(loc).rglob("*.py") if "__pycache__" not in p.parts)
        elif spec.origin and spec.origin.endswith(".py"):
            files.append(Path(spec.origin))
    return sorted(set(files))


def source_version(area: Area) -> str:
    """`v-<hash>` of the area's source files (names and bytes)."""
    import hashlib

    digest = hashlib.sha256()
    for path in source_files(area):
        try:
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
        except OSError:
            continue
    return "v-" + digest.hexdigest()[:12]


def area_owning(path: Any) -> Optional[Area]:
    """The area whose source contains this file, if any."""
    from pathlib import Path

    target = Path(path).resolve()
    for area in AREAS.values():
        for f in source_files(area):
            if f.resolve() == target:
                return area
    return None


def _bind(mods: dict) -> None:
    """Point each module's parent package at it (the import system does this on import, not on sys.modules edits)."""
    for name, mod in mods.items():
        parent, _, tail = name.rpartition(".")
        if parent and parent in sys.modules:
            try:
                setattr(sys.modules[parent], tail, mod)
            except Exception:  # noqa: BLE001
                pass


def _forget(area: Area) -> None:
    for name in [n for n in sys.modules if _under(n, area_packages(area))]:
        del sys.modules[name]


def purge(area: Area) -> None:
    """Make the next load of an area that is not running import its code afresh (no import, no tool registration)."""
    _forget(area)
    RECORDED.pop(area.id, None)
    BOOT.pop(area.id, None)


def fresh_import(area: Area) -> tuple:
    """Import the area's code afresh into private module objects without disturbing what is live: sys.modules and
    the tool registry are put back as they were before returning. Returns (modules, tool entries). Raises if the new
    source does not import. The caller installs the modules at commit (`install`)."""
    from bot.agent_runtime import toolspec

    prefixes = area_packages(area)
    # The global import lock (re-entrant for this thread) keeps other threads from importing the area while its live
    # modules are out of sys.modules: they wait and then see the live ones again, never a half-swapped package.
    _imp.acquire_lock()
    old = {n: m for n, m in sys.modules.items() if _under(n, prefixes)}
    tools_before = dict(toolspec._registered)
    for name in old:
        del sys.modules[name]
    try:
        for name in list(area.tools) + [t.partition(":")[0] for t in area.tasks] + ([area.api] if area.api else []) + list(old):
            mod = importlib.import_module(name)
            if name in area.tools and hasattr(mod, "register_tools") and not getattr(mod, "REGISTER_ON_IMPORT", True):
                mod.register_tools()
        new = {n: m for n, m in sys.modules.items() if _under(n, prefixes)}
        entries = {n: e for n, e in toolspec._registered.items() if tools_before.get(n) is not e}
        return new, entries
    finally:
        _forget(area)
        sys.modules.update(old)
        _bind(old)
        for name in [n for n in toolspec._registered if n not in tools_before]:
            toolspec._registered.pop(name, None)
        toolspec._registered.update(tools_before)
        _imp.release_lock()


def install(mods: dict) -> dict:
    """Make these modules the ones importers get (commit time). Returns what they replaced, for `install(previous)`."""
    previous = {n: sys.modules[n] for n in mods if n in sys.modules}
    sys.modules.update(mods)
    _bind(mods)
    reset_static()
    return previous


def scrub_inactive_tools() -> list[str]:
    """Unregister the tools of areas that are not running (a module reload may have registered them as an import
    side effect); returns the names removed."""
    from bot.agent_runtime import toolspec
    from bot.runtime import live

    removed: list[str] = []
    for area_id, entries in list(KNOWN_TOOLS.items()):
        if live.state_of(area_id) == "active":
            continue
        for name in entries:
            if toolspec._registered.pop(name, None) is not None:
                removed.append(name)
    return removed


def area_tools(area: Area) -> list:
    """The (schema, spec, handler, enabled) entries of an area: the recorded ones, importing first if needed."""
    if not area.tools:
        return []
    entries = RECORDED.get(area.id) or _import_tools(area)
    return list(entries.values())


def restore_tools(unit_ids: Any) -> None:
    """Register again the recorded tools of these areas (after the engine stopped and unregistered them)."""
    from bot.agent_runtime import toolspec

    for uid in unit_ids:
        for name, entry in RECORDED.get(uid, {}).items():
            toolspec._registered.setdefault(name, entry)


def tool_names(area: Area) -> list[str]:
    return [entry[1].name for entry in area_tools(area)]


# ------------------------------------------------------------------ routes
# unit id -> (args, kwargs) handed to the area's `register(app, *args, **kwargs)`: the dashboard's auth dependencies
# (bot/dashboard/server.py `_area_routes`, run by build_app).
API_AUTH: dict[str, tuple] = {}


def set_api_auth(units: Any, args: tuple, kwargs: dict) -> None:
    for unit in ((units,) if isinstance(units, str) else units):
        API_AUTH[unit] = (tuple(args), dict(kwargs))
    reset_static()


def _under_prefix(path: str, prefixes: Any) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def area_for_path(path: str) -> Optional[str]:
    """The id of the converted area whose route prefix `path` falls under (the longest prefix), if any."""
    best: Optional[tuple] = None
    for area in AREAS.values():
        if not area.converted:
            continue
        for prefix in area.routes:
            if _under_prefix(path, (prefix,)) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), area.id)
    return best[1] if best else None


def build_router(area: Area, mod: Any = None) -> Any:
    """Run the area's `register(app, *auth)` against a collector and return the router it filled (None-free: an
    area without routes gives an empty router). `mod` is the (possibly freshly imported) route module. An area that
    shares its module with another (localai / neurallab) keeps only the routes under its own prefixes."""
    from fastapi import APIRouter

    from bot.runtime.pluggables import _Collector

    args, kwargs = API_AUTH.get(area.id, ((), {}))
    mod = mod or importlib.import_module(area.api)
    collector = _Collector()
    mod.register(collector, *args, **kwargs)
    if sum(1 for a in AREAS.values() if a.api == area.api) > 1:
        shared = APIRouter()
        shared.routes.extend(r for r in collector.routes if _under_prefix(getattr(r, "path", ""), area.routes))
        return shared
    return collector


# Routes served without a booted engine (an app used without its lifespan, and the OpenAPI document): every area's
# router built from the modules as loaded now, held in one plain app.
_static: dict = {}


def reset_static() -> None:
    _static.clear()


def static_routers() -> dict:
    if "routers" not in _static:
        routers: dict = {}
        for area in AREAS.values():
            if not area.api or not area.converted:
                continue
            try:
                routers[area.id] = build_router(area)
            except Exception:  # noqa: BLE001 - a broken area costs that area only
                logger.exception("routes of %s could not be built", area.id)
        _static["routers"] = routers
    return _static["routers"]


def static_app() -> Any:
    if "app" not in _static:
        from fastapi import FastAPI

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        for router in static_routers().values():
            app.include_router(router)
        _static["app"] = app
    return _static["app"]


def doc_routes() -> list:
    """Every area's routes, for the OpenAPI document (they are served by the dispatcher, not registered on the app)."""
    return [r for router in static_routers().values() for r in router.routes]


# bot/main.py sets this: background tasks then run only while this process holds the leader lease (as the singletons
# they replace did), and stop when it is given up. Off (default), tasks run as soon as the unit is active.
LEADER_GATED = False
_LEADER_POLL = 1.0


def _is_leader() -> bool:
    from bot import lease

    ctl = lease.controller()
    return bool(ctl is not None and ctl.running)


def _task(target: str):
    mod_name, _, fn_name = target.partition(":")

    async def run_once() -> None:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        stop = asyncio.Event()
        try:
            await fn(stop)
        finally:
            stop.set()

    async def run() -> None:
        if not LEADER_GATED:
            await run_once()
            return
        while True:
            while not _is_leader():  # noqa: ASYNC110 - the lease has no event to wait on; it is polled
                await asyncio.sleep(_LEADER_POLL)
            inner = asyncio.ensure_future(run_once())
            try:
                while not inner.done() and _is_leader():
                    await asyncio.wait({inner}, timeout=_LEADER_POLL)
                if inner.done():
                    inner.result()
                    return
            finally:
                if not inner.done():
                    inner.cancel()
                    await asyncio.gather(inner, return_exceptions=True)

    run.__name__ = fn_name
    return run


class _CodeSwap:
    """A registration: at commit the version's freshly imported modules become the ones importers get; an instance
    stopped without ever having been committed (an aborted transaction) never installed anything."""

    def __init__(self, unit: "LegacyUnit") -> None:
        self.unit = unit

    def apply(self) -> None:
        u = self.unit
        if u._mods:
            install(u._mods)
        u._applied = True
        if u._entries is not None:
            RECORDED[u.area.id] = dict(u._entries)
            KNOWN_TOOLS.setdefault(u.area.id, set()).update(u._entries)

    def revert(self) -> None:
        return None


class LegacyUnit:
    """The Unit contract over an existing feature area. A version other than the boot version "1" (`v-<hash>`) is a
    hot-reloaded one: its code is imported afresh while the old version keeps serving, and installed at commit."""

    def __init__(self, area: Area, version: str = "1") -> None:
        self.id = area.id
        self.area = area
        self.version = version
        self._mods: dict = {}
        self._entries: Any = None
        self._applied = False

    def _load(self) -> list:
        a = self.area
        if self._entries is None:
            if self.version.startswith("v-"):
                now = source_version(a)
                if now != self.version:
                    raise LookupError(f"the source of {a.id} is no longer at version {self.version} (it is {now})")
                self._mods, self._entries = fresh_import(a)
            else:
                if a.id not in BOOT or a.id not in RECORDED:                # version "1" is the code as first loaded, whatever runs now
                    if a.api:
                        importlib.import_module(a.api)
                    entries = {e[1].name: e for e in area_tools(a)}
                    prefixes = area_packages(a)
                    BOOT[a.id] = (entries, {n: m for n, m in sys.modules.items() if _under(n, prefixes)})
                self._entries, self._mods = dict(BOOT[a.id][0]), dict(BOOT[a.id][1])
        return list(self._entries.values())

    def contributions(self, ctx: Any) -> Contributions:
        a = self.area
        panel = {"id": a.id, "title": a.title, "group": a.group, "icon": a.icon, "order": a.order,
                 "kind": "builtin", "section": a.section or a.id}
        from bot.runtime import live

        error = live.failure_of(a.id)
        if error:
            raise RuntimeError(f"routes of {a.id} could not be registered: {error}")
        tools = self._load()
        router = None
        if a.api and a.converted:
            built = build_router(a, self._mods.get(a.api))
            router = built if built.routes else None
        return Contributions(panels=[panel] if a.panel else [], tools=tools, routes=router,
                             route_prefixes=list(a.routes) if router is not None else [],
                             tasks=[_task(t) for t in a.tasks], registrations=[_CodeSwap(self)])

    async def start(self, ctx: Any) -> None:
        self._applied = False

    async def health(self, ctx: Any) -> Health:
        return Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        return None


def register_factories(factories: Any) -> None:
    for area in AREAS.values():
        factories.register(area.id, lambda spec, a=area: LegacyUnit(a, spec.version))
