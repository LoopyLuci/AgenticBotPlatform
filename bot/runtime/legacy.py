"""Adapter that turns an existing feature area into a unit (docs/runtime.md, phase 2).

An area is declared as one `Area` entry in `AREAS`: its panel (the `data-unit` / section id of its dashboard page),
the route prefixes its FastAPI routes live under, the modules whose import registers its agent tools and the
`module:function` of its background `run_forever(stop_event)` singletons. The adapter imports all of that lazily,
inside `contributions()`, so an area that is not loaded costs nothing and one whose import fails is reported failed
by the engine without touching ABP or any other unit.

Routes registered on the FastAPI app cannot be removed, so `UnitGate` (an ASGI middleware, see bot/runtime/live.py)
answers 404 `unit_unavailable` for an area's route prefixes while its unit is not active.

Areas with `converted=False` only list their page for now (so the UI keeps showing it); their tools, tasks and routes
are still wired unconditionally and move into the table entry when the area is converted.
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


def _area(id, title, icon, order, section="", **kw) -> tuple:
    return id, Area(id, title, icon, order=order, section=section or id, **kw)


AREAS: dict[str, Area] = dict([
    # -- converted: routes, tools (and tasks) belong to the unit
    _area("vm-harness", "VM-Harness", "🖧", 182, routes=("/api/vm-harness",), tools=("bot.vm_harness.tools",)),
    _area("hermes-manager", "Hermes Manager", "☤", 183, routes=("/api/hermes-manager",),
          tools=("bot.hermes_manager.tools",)),
    _area("ollama", "Ollama", "🦙", 184, routes=("/api/ollama",), tools=("bot.ollama.tools",)),
    # -- listed only (page visibility); the next worker moves their routes, tools and tasks into the entry
    _area("router", "Model Router", "🧭", 160, converted=False),
    _area("unsloth", "Unsloth", "🦥", 161, converted=False),
    _area("cluster", "Cluster", "⧉", 162, converted=False),
    _area("private_project", "PrivateProject", "🐙", 163, converted=False),
    _area("vision", "Vision", "👁", 164, converted=False),
    _area("hosting", "Hosting", "🌐", 165, converted=False),
    _area("storage", "Storage", "🗄", 166, converted=False),
    _area("localai", "Local AI", "🦙", 167, converted=False),
    _area("neurallab", "Neural Lab", "🧠", 168, section="lab", converted=False),
    _area("studio", "Studio", "🎨", 169, converted=False),
    _area("modules", "Modules", "▦", 170, converted=False),
    _area("transferdaemon", "TransferDaemon", "⇄", 171, converted=False),
    _area("power", "Power", "⏻", 172, converted=False),
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


def tool_names(area: Area) -> list[str]:
    return [entry[1].name for entry in area_tools(area)]


def _task(target: str):
    mod_name, _, fn_name = target.partition(":")

    async def run() -> None:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        stop = asyncio.Event()
        try:
            await fn(stop)
        finally:
            stop.set()

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
        return Contributions(panels=[panel], tools=area_tools(a), tasks=[_task(t) for t in a.tasks])

    async def start(self, ctx: Any) -> None:
        return None

    async def health(self, ctx: Any) -> Health:
        return Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        return None


def register_factories(factories: Any) -> None:
    for area in AREAS.values():
        factories.register(area.id, lambda spec, a=area: LegacyUnit(a))
