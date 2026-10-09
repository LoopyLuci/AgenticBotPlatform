"""The contract a unit implements, what it contributes, and the runtime's record of one running instance.

`Contributions` is data, not side effects: the runtime registers it and can therefore unregister it. A unit never
registers routes or tools itself and never starts bare threads or `asyncio.create_task`; it hands over a router,
tool triples and task callables, and `ctx.spawn` runs anything else through the task supervisor.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

DEFAULT_INSTANCE = "default"

# Instance slots (docs/runtime.md, "Concepts").
ACTIVE, STAGED, STANDBY, DRAINING, STOPPED = "active", "staged", "standby", "draining", "stopped"


def make_ref(unit: str, instance: str = DEFAULT_INSTANCE) -> str:
    """`unit@instance`, the key every table in the runtime uses."""
    return f"{unit}@{instance or DEFAULT_INSTANCE}"


def split_ref(ref: str) -> tuple[str, str]:
    unit, _, instance = ref.partition("@")
    return unit, instance or DEFAULT_INSTANCE


@dataclass
class Health:
    status: str = "ok"           # ok | degraded | failed
    detail: str = ""

    @property
    def passes(self) -> bool:
        return self.status in ("ok", "degraded")

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


@dataclass
class Contributions:
    routes: Any = None                                        # an APIRouter (or None)
    tools: list = field(default_factory=list)                 # (schema, ToolSpec, handler[, enabled]) as toolspec.register takes
    panels: list[dict] = field(default_factory=list)          # id, title, nav group, icon, script, styles, order
    cli: list = field(default_factory=list)
    tasks: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)
    config_overlay: dict = field(default_factory=dict)        # defaults merged into config while the instance is live
    route_prefixes: list = field(default_factory=list)        # absolute /api/... prefixes the routes answer at (extensions)
    registrations: list = field(default_factory=list)         # objects with apply() / revert(): run at commit, sync, idempotent


class Unit(Protocol):
    """What a unit implements. `export_state` / `import_state` are optional (a swap skips what is missing).
    `stop`'s deadline is an absolute `time.monotonic()` value. A stopped unit may be started again with the same
    object (a rollback reuses the standby build), so `start` must reopen what `stop` closed."""

    id: str

    def contributions(self, ctx: "UnitContext") -> Contributions: ...
    async def start(self, ctx: "UnitContext") -> None: ...
    async def health(self, ctx: "UnitContext") -> Health: ...
    async def stop(self, ctx: "UnitContext", *, deadline: float) -> None: ...


class UnitContext:
    """What the runtime hands a unit: its identity, durable state, a logger and supervised task spawning."""

    def __init__(self, unit: str, instance: str, version: str, settings: dict, state: Any,
                 spawn: Callable[..., Any]) -> None:
        self.unit = unit
        self.instance = instance
        self.version = version
        self.settings = settings
        self.state = state                       # a StateNamespace (get / set / delete)
        self.logger = logging.getLogger(f"bot.unit.{unit}@{instance}")
        self._spawn = spawn

    @property
    def ref(self) -> str:
        return make_ref(self.unit, self.instance)

    def spawn(self, coro_fn: Callable[[], Awaitable[Any]], name: Optional[str] = None) -> Any:
        """Run `coro_fn()` in this instance's task group; it is cancelled when the instance stops."""
        return self._spawn(coro_fn, name)


class Instance:
    """One built copy of a unit (a version of `unit@instance`) with its slot and live counters."""

    def __init__(self, ref: str, version: str, settings: dict, unit: Any, ctx: UnitContext, group: str) -> None:
        self.ref = ref
        self.unit_id, self.instance_id = split_ref(ref)
        self.version = version
        self.settings = settings
        self.spec: Any = None                    # the InstanceSpec this was built from
        self.unit = unit
        self.ctx = ctx
        self.group = group                       # task group name (unique per build)
        self.contributions = Contributions()
        self.app: Any = None                     # ASGI app serving contributions.routes, or None
        self.slot = STAGED
        self.health: Optional[Health] = None
        self.started = False
        self.from_standby = False
        self.built_at = time.time()
        self.inflight = 0                        # HTTP requests inside the instance
        self.connections = 0                     # pinned WebSocket connections
        self._idle = asyncio.Event()
        self._idle.set()

    def enter(self, websocket: bool = False) -> None:
        if websocket:
            self.connections += 1
        else:
            self.inflight += 1
        self._idle.clear()

    def leave(self, websocket: bool = False) -> None:
        if websocket:
            self.connections -= 1
        else:
            self.inflight -= 1
        if self.inflight + self.connections <= 0:
            self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), max(timeout, 0.0))
            return True
        except asyncio.TimeoutError:
            return False

    def describe(self) -> dict[str, Any]:
        return {"ref": self.ref, "unit": self.unit_id, "instance": self.instance_id, "version": self.version,
                "slot": self.slot, "health": self.health.to_dict() if self.health else None,
                "inflight": self.inflight, "connections": self.connections}
