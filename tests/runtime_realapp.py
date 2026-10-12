"""The REAL dashboard app (bot.dashboard.server.build_app) served by a real uvicorn on a free port, with a temporary
data folder, for the guarantee tests (test_runtime_guarantees.py, test_runtime_ui_real.py).

`real_app` is a fixture factory: `real_app(units="all")` returns a started RealApp; it is stopped at teardown. The
areas' background tasks are replaced by idle stand-ins (hosting, DNS, file server ... must not do real work in a
test) and the private folder points into the temp folder, so nothing outside it is read or written."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
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


# Ports Chromium refuses to load (ERR_UNSAFE_PORT): a browser test on one of them cannot work.
UNSAFE_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110,
    111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061,
    6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080})


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in UNSAFE_PORTS:
            return port


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
    monkeypatch.setattr(legacy, "TASKS_ENABLED", True)  # the stand-ins below are idle
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

    # A 5xx in the server is only a status code in the test; keep the server-side traceback so the cause is visible.
    class _Errors(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.ERROR)
            self.found: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.found.append(self.format(record))
    errors = _Errors()
    errors.setFormatter(logging.Formatter("%(message)s"))
    uvicorn_log = logging.getLogger("uvicorn.error")
    uvicorn_log.addHandler(errors)

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
    uvicorn_log.removeHandler(errors)
    if errors.found:
        # Printed (pytest shows captured output of a failing test) and logged; a passing test is not failed by it.
        print("SERVER-SIDE ERRORS:\n" + "\n---\n".join(errors.found))
    # Put back what was there; keep what the app imported meanwhile (area tool modules register once, at import,
    # and stay imported - wiping them left later tests in this process without those tools).
    toolspec._registered.update(saved_tools)
