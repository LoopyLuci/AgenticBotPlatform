"""Adapter that turns an existing feature area into a unit (docs/runtime.md, phase 2).

An area is declared as one `Area` entry in `AREAS`: its panel (the `data-unit` / section id of its dashboard page),
the route prefixes its FastAPI routes live under, the modules whose import registers its agent tools and the
`module:function` of its background `run_forever(stop_event)` singletons. The adapter imports all of that lazily,
inside `contributions()`, so an area that is not loaded costs nothing and one whose import fails is reported failed
by the engine without touching ABP or any other unit.

Routes registered on the FastAPI app cannot be removed, so `UnitGate` (an ASGI middleware, see bot/runtime/live.py)
answers 404 `unit_unavailable` for an area's route prefixes while its unit is not active.

Every area is converted. `converted=False` (an entry that only lists its page) and `panel=False` (an area without a
dashboard page) remain for entries added later or by tests. Route registration (bot/dashboard/server.py) records an
import/registration failure with `bot.runtime.live.record_failure`; the unit then fails at boot with that error.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from dataclasses import dataclass
from typing import Any

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


def _area(id, title, icon, order, section="", **kw) -> tuple:
    return id, Area(id, title, icon, order=order, section=section or id, **kw)


AREAS: dict[str, Area] = dict([
    # every feature area: its routes, tools and tasks belong to the unit
    _area("vm-harness", "VM-Harness", "🖧", 182, routes=("/api/vm-harness",), tools=("bot.vm_harness.tools",)),
    _area("hermes-manager", "Hermes Manager", "☤", 183, routes=("/api/hermes-manager",),
          tools=("bot.hermes_manager.tools",)),
    _area("ollama", "Ollama", "🦙", 184, routes=("/api/ollama",), tools=("bot.ollama.tools",)),
    _area("router", "Model Router", "🧭", 160, routes=("/api/router",), tools=("bot.model_router",)),
    _area("unsloth", "Unsloth", "🦥", 161, routes=("/api/unsloth",), tools=("bot.unsloth.tools",)),
    _area("cluster", "Cluster", "⧉", 162, routes=("/api/cluster",), tools=("bot.cluster.tools",),
          tasks=("bot.cluster.membership:heartbeat_forever",)),
    _area("private_project", "PrivateProject", "🐙", 163, routes=("/api/private_project",)),
    _area("vision", "Vision", "👁", 164, routes=("/api/vision",), tools=("bot.vision.tools",)),
    _area("hosting", "Hosting", "🌐", 165, routes=("/api/hosting",), tools=("bot.hosting.tools",),
          tasks=("bot.hosting.service:run_forever",)),
    _area("storage", "Storage", "🗄", 166, routes=("/api/fileserver",), tools=("bot.fileserver.tools",),
          tasks=("bot.fileserver.service:run_forever",)),
    _area("localai", "Local AI", "🦙", 167, routes=("/api/localai",), tools=("bot.localai.tools",),
          tasks=("bot.localai.service:run_forever",)),
    _area("neurallab", "Neural Lab", "🧠", 168, section="lab", routes=("/api/lab",),
          tasks=("bot.neurallab.service:run_forever",)),
    _area("studio", "Studio", "🎨", 169, routes=("/api/studio", "/studio/v")),
    _area("modules", "Modules", "▦", 170, routes=("/api/modules",), tools=("bot.modules.tools",)),
    _area("transferdaemon", "TransferDaemon", "⇄", 171, routes=("/api/transferdaemon",),
          tools=("bot.transferdaemon.tools",)),
    _area("power", "Power", "⏻", 172, routes=("/api/power",), tools=("bot.power_tools",)),
    # the memory fabric has no page of its own: routes and the upkeep task only
    _area("memoryfabric", "Memory Fabric", "", 173, routes=("/api/memory",),
          tasks=("bot.memoryfabric.service:run_forever",), panel=False),
])


# area id -> {tool name: (schema, spec, handler, enabled)} -- what importing the area's tools modules registered
RECORDED: dict[str, dict] = {}


def _import_tools(area: Area) -> dict:
    """Import the area's tools modules (they register their tools as an import side effect, like every registering
    module) and record the entries that import added or replaced. Raises if an import fails."""
    from bot.agent_runtime import toolspec

    before = dict(toolspec._registered)
    for name in area.tools:
        already = name in sys.modules
        mod = importlib.import_module(name)
        if already and hasattr(mod, "register_tools"):
            mod.register_tools()                  # imported earlier (maybe unregistered since): register again
    entries = {n: e for n, e in toolspec._registered.items() if before.get(n) is not e}
    if entries:
        RECORDED.setdefault(area.id, {}).update(entries)
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
            while not _is_leader():
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


class LegacyUnit:
    """The Unit contract over an existing feature area."""

    def __init__(self, area: Area) -> None:
        self.id = area.id
        self.area = area

    def contributions(self, ctx: Any) -> Contributions:
        a = self.area
        panel = {"id": a.id, "title": a.title, "group": a.group, "icon": a.icon, "order": a.order,
                 "kind": "builtin", "section": a.section or a.id}
        from bot.runtime import live

        error = live.failure_of(a.id)
        if error:
            raise RuntimeError(f"routes of {a.id} could not be registered: {error}")
        return Contributions(panels=[panel] if a.panel else [], tools=area_tools(a),
                             tasks=[_task(t) for t in a.tasks])

    async def start(self, ctx: Any) -> None:
        return None

    async def health(self, ctx: Any) -> Health:
        return Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        return None


def register_factories(factories: Any) -> None:
    for area in AREAS.values():
        factories.register(area.id, lambda spec, a=area: LegacyUnit(a))
