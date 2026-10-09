"""Toy units and helpers shared by tests/test_runtime_*.py."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, WebSocket

from bot.agent_runtime import toolspec
from bot.runtime.dispatcher import Dispatcher
from bot.runtime.engine import RuntimeEngine, UnitFactories
from bot.runtime.manifest import BranchStore, InstanceSpec, Manifest
from bot.runtime.state import StateStore
from bot.runtime.unit import Contributions, Health


class Toy:
    """A unit whose behaviour is its version: GET /x -> {"v": version, "n": calls}, a tool, a panel, a task."""

    stops = 0
    log: list = []

    def __init__(self, spec: InstanceSpec, health: str = "ok", delay: float = 0.0) -> None:
        self.id = spec.unit
        self.spec = spec
        self.health_status = spec.settings.get("health", health)
        self.delay = delay
        self.memory = 0
        self.ticks = 0
        self.stopped = 0
        self.started = 0

    def contributions(self, ctx):
        router = APIRouter()
        version = ctx.version

        @router.get("/x")
        async def x():
            await asyncio.sleep(self.delay or 0.002)     # real work yields to the loop
            n = (ctx.state.get("n") or 0) + 1
            ctx.state.set("n", n)
            return {"v": version, "n": n, "instance": ctx.instance}

        @router.websocket("/ws")
        async def ws(sock: WebSocket):
            await sock.accept()
            try:
                while True:
                    await sock.receive_text()
                    await sock.send_text(version)
            except Exception:  # noqa: BLE001 - client went away
                return

        async def handler(tool_input, **_ctx):
            return f"{self.id}:{version}"

        name = f"toy_{self.id.replace(':', '_').replace('-', '_')}"
        schema = {"name": name, "description": "toy", "input_schema": {"type": "object", "properties": {}}}

        async def tick():
            while True:
                self.ticks += 1
                await asyncio.sleep(0.005)

        return Contributions(
            routes=router, tools=[(schema, toolspec.ToolSpec(name, "read", True, True), handler)],
            panels=[{"id": f"{self.id}-panel", "title": self.id, "order": 5}],
            tasks=[tick], config_overlay={"toy": {self.id: version}})

    async def start(self, ctx):
        self.started += 1

    async def health(self, ctx):
        return Health(self.health_status, "toy says " + self.health_status)

    async def stop(self, ctx, *, deadline):
        self.stopped += 1
        Toy.log.append(("stop", ctx.ref, ctx.version))

    def export_state(self, ctx):
        return {"memory": self.memory}

    def import_state(self, ctx, state):
        self.memory = state.get("memory", 0)


def make_engine(tmp_path: Path, units=("alpha",), **kwargs) -> RuntimeEngine:
    factories = UnitFactories()
    for unit in units:
        factories.register(unit, Toy)
    kwargs.setdefault("drain_deadline", 5.0)
    return RuntimeEngine(factories=factories, store=StateStore(tmp_path / "state.db"),
                         branches=BranchStore(tmp_path / "branches"), dispatcher=Dispatcher(), **kwargs)


def client(engine: RuntimeEngine) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=engine.dispatcher), base_url="http://t")


def manifest(**refs: tuple) -> Manifest:
    """manifest(alpha=("1",), ...) with refs written unit__instance for non-default ones."""
    out = Manifest()
    for key, (version, *rest) in refs.items():
        unit, _, inst = key.partition("__")
        out = out.with_instance(unit, inst or "default", InstanceSpec(unit, version, rest[0] if rest else {}))
    return out


def thread_names() -> set:
    return {t.name for t in threading.enumerate()}


class WS:
    """A hand-driven ASGI websocket client against any ASGI app (no network)."""

    def __init__(self, app, path: str) -> None:
        self.app, self.path = app, path
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.task = None

    async def open(self) -> None:
        scope = {"type": "websocket", "path": self.path, "raw_path": self.path.encode(), "root_path": "",
                 "query_string": b"", "headers": [], "scheme": "ws", "subprotocols": [], "server": ("t", 80),
                 "client": ("c", 1)}
        await self.inbox.put({"type": "websocket.connect"})
        self.task = asyncio.get_running_loop().create_task(self.app(scope, self.inbox.get, self.outbox.put))
        msg = await asyncio.wait_for(self.outbox.get(), 2)
        assert msg["type"] == "websocket.accept", msg

    async def ask(self) -> str:
        await self.inbox.put({"type": "websocket.receive", "text": "hi"})
        msg = await asyncio.wait_for(self.outbox.get(), 2)
        return msg["text"]

    async def close(self) -> None:
        await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)
