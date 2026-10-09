"""The REAL dashboard app (bot.dashboard.server.build_app) served by a real uvicorn on a free port, with a temporary
data folder, for the guarantee tests (test_runtime_guarantees.py, test_runtime_ui_real.py).

`real_app` is a fixture factory: `real_app(units="all")` returns a started RealApp; it is stopped at teardown. The
areas' background tasks are replaced by idle stand-ins (hosting, DNS, file server ... must not do real work in a
test) and the private folder points into the temp folder, so nothing outside it is read or written."""
from __future__ import annotations

import asyncio
import dataclasses
import socket
import sys
import threading
import time
import types
from typing import Any, Callable, Optional

import httpx
import pytest

from bot.agent_runtime import toolspec
from bot.runtime import legacy, live

TOKEN = "test-token"
H = {"X-Dashboard-Token": TOKEN}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _idle(stop_event):
    await asyncio.Event().wait()


class RealApp:
    def __init__(self, server: Any, thread: threading.Thread, port: int) -> None:
        self.server, self.thread, self.port = server, thread, port
        self.url = f"http://127.0.0.1:{port}"
        self.http = httpx.Client(base_url=self.url, headers=H, timeout=30.0)

    # -- HTTP helpers
    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)

    def post(self, path: str, json: Optional[dict] = None, **kw) -> httpx.Response:
        return self.http.post(path, json=json if json is not None else {}, **kw)

    def status(self) -> dict:
        return self.get("/api/runtime").json()

    def generation(self) -> int:
        return self.status()["generation"]

    def units(self) -> set:
        return {u["unit"] for u in self.status()["units"]}

    def panels(self) -> list:
        return self.get("/api/ui/manifest").json()["panels"]

    def builtin_units(self) -> set:
        return {p["unit"] for p in self.panels() if p["kind"] == "builtin"}

    def unload(self, unit: str, **body) -> httpx.Response:
        return self.post(f"/api/runtime/units/{unit}/unload", body)

    def load(self, unit: str, **body) -> httpx.Response:
        return self.post(f"/api/runtime/units/{unit}/load", body)

    def swap(self, unit: str, version: str, **body) -> httpx.Response:
        return self.post(f"/api/runtime/units/{unit}/swap", {"version": version, **body})

    # -- the server's own loop
    def on_loop(self, fn: Callable, timeout: float = 30.0) -> Any:
        """Run `async fn()` on the loop the app runs on and return its result."""
        return asyncio.run_coroutine_threadsafe(fn(), live._loop).result(timeout)

    def engine(self):
        return live.get_engine()

    def toy_units(self, *ids: str) -> None:
        """Register the toy unit (tests/runtime_toys.py) under these ids in the live engine."""
        from runtime_toys import Toy
        for unit in ids:
            self.engine().factories.register(unit, Toy)

    def settle(self) -> None:
        async def go():
            await self.engine().settle()
        self.on_loop(go)

    def tasks_and_threads(self) -> tuple:
        """(asyncio tasks alive on the app's loop, thread names), after letting drains and cancellations finish."""
        self.settle()

        async def count():
            await asyncio.sleep(0.05)
            return len([t for t in asyncio.all_tasks() if not t.done()])
        return self.on_loop(count), sorted(t.name for t in threading.enumerate())

    def stop(self) -> None:
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture
def real_app(monkeypatch, temp_db, tmp_path):
    import uvicorn

    from bot.dashboard.server import build_app
    from bot.hermes_manager import harness as hm
    from bot.ollama import harness as ol
    from bot.vm_harness import harness as vm

    saved_tools = dict(toolspec._registered)
    fake = types.ModuleType("abp_test_idle_tasks")
    fake.idle = _idle
    monkeypatch.setitem(sys.modules, "abp_test_idle_tasks", fake)
    for uid, area in list(legacy.AREAS.items()):
        if area.tasks:
            monkeypatch.setitem(legacy.AREAS, uid, dataclasses.replace(
                area, tasks=tuple("abp_test_idle_tasks:idle" for _ in area.tasks)))
    private = tmp_path / "private"
    (private / "extensions").mkdir(parents=True)
    monkeypatch.setenv("ABP_PRIVATE_DIR", str(private))
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setattr(vm, "status", lambda backends=True: {"unit": "vm"})
    monkeypatch.setattr(hm, "status", lambda: {"unit": "hm"})
    monkeypatch.setattr(ol, "status", lambda: {})
    monkeypatch.setattr(ol, "summary", lambda full=None: {"unit": "ol"})
    started: list[RealApp] = []

    def start(units: str = "all") -> RealApp:
        monkeypatch.setenv("ABP_UNITS", units)
        live.reset()
        live.clear_failures()
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True, name="abp-test-uvicorn")
        thread.start()
        deadline = time.time() + 60
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "the real app did not start"
        app = RealApp(server, thread, port)
        started.append(app)
        return app

    yield start
    for app in started:
        app.stop()
    live.reset()
    live.clear_failures()
    toolspec._registered.clear()
    toolspec._registered.update(saved_tools)
