"""Extensions, Python plugins, WASM plugins and external MCP servers as runtime units (bot/runtime/pluggables.py).

Real objects throughout: a temporary extensions folder (never the real private folder), real plugin files, a real
engine, dispatcher and swap endpoint, a real stdio MCP server subprocess. The one stand-in is the native WASM
engine (no compiled component is available here): a fake `Runtime.shared()` hands back handles that go through the
real `LoadedPlugin` wrapper, manager and tool registration.

For each kind: load -> things present; swap to a broken version -> old still serving, generation unchanged; swap to
a good version -> new behaviour, old drained; unload -> nothing left.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from bot import db, extensions, plugins
from bot.agent_runtime import mcp_client, toolspec
from bot.runtime import pluggables
from bot.runtime.api import build_router
from bot.runtime.dispatcher import Dispatcher
from bot.runtime.engine import RuntimeEngine, UnitFactories
from bot.runtime.manifest import BranchStore
from bot.runtime.state import StateStore


def _noauth() -> None:
    return None


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.probe = tmp_path / "probe.log"
        factories = UnitFactories()
        pluggables.register_factories(factories)
        self.engine = RuntimeEngine(factories=factories, store=StateStore(tmp_path / "state.db"),
                                    branches=BranchStore(tmp_path / "branches"), dispatcher=Dispatcher(),
                                    drain_deadline=5.0)
        self.engine.version_resolver = pluggables.version_of
        api_app = FastAPI()
        api_app.include_router(build_router(self.engine, _noauth, _noauth))
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://t")
        self.units = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.engine.dispatcher), base_url="http://t")

    def probe_lines(self) -> list[str]:
        return self.probe.read_text().splitlines() if self.probe.exists() else []

    async def swap(self, unit: str) -> httpx.Response:
        return await self.api.post(f"/api/runtime/units/{unit}/swap", json={})

    async def panels(self) -> list[dict]:
        return (await self.api.get("/api/ui/manifest")).json()["panels"]


@pytest.fixture
def env(tmp_path, monkeypatch, temp_db):
    monkeypatch.setenv("ABP_PRIVATE_DIR", str(tmp_path / "private"))
    (tmp_path / "private" / "extensions").mkdir(parents=True)
    monkeypatch.setenv("SAMPLE_PROBE", str(tmp_path / "probe.log"))
    pluggables.reset()
    pluggables.set_auth(_noauth, _noauth)
    import bot.agent_runtime.tools  # noqa: F401 - registers the areas' tools now, so they are part of `saved`
    saved = dict(toolspec._registered)
    e = Env(tmp_path)
    yield e
    pluggables.reset()
    pluggables._owners.clear()
    mcp_client.runtime_owned.clear()
    for name in set(toolspec._registered) - set(saved):
        toolspec.unregister(name)
    extensions.load_all(force=True)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------------------------- extensions
EXT_ENTRY = '''\
import asyncio
import os


def _log(message):
    with open(os.environ["SAMPLE_PROBE"], "a") as fh:
        fh.write(message + "\\n")


def register(api):
    word = "{word}"
    {broken}

    def add_routes(app, read_auth, write_auth):
        from fastapi import Depends

        @app.get("/api/sample/ping", dependencies=[Depends(read_auth)])
        async def ping():
            return {{"word": word}}

        @app.get("/api/sample/slow")
        async def slow():
            await asyncio.sleep(0.4)
            return {{"word": word}}

    api.add_routes(add_routes)
    api.add_panel("sample", "Sample")
    api.add_backend("sample_backend", kind="cli", command="sample --json")

    async def sample_tool(text: str = "") -> dict:
        """Echo the text with this version's word."""
        return {{"word": word, "text": text}}

    api.add_mcp_tool(sample_tool, "sample_tool")

    async def run():
        try:
            while True:
                _log("tick-" + word)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            _log("cancelled-" + word)
            raise

    api.add_task(run)
'''


def write_ext(env: Env, word: str, broken: bool = False) -> Path:
    folder = env.tmp / "private" / "extensions" / "sample"
    (folder / "sample_ext").mkdir(parents=True, exist_ok=True)
    (folder / "abp_extension.toml").write_text(
        '[extension]\nname = "sample"\nversion = "1.0.0"\ndescription = "A neutral sample extension."\n'
        'entry = "sample_ext"\n', encoding="utf-8")
    body = EXT_ENTRY.format(word=word, broken='raise RuntimeError("broken on purpose")' if broken else "pass")
    (folder / "sample_ext" / "__init__.py").write_text(body, encoding="utf-8")
    (folder / "panel.js").write_text(f"// panel {word}\n", encoding="utf-8")
    return folder


async def ping(env: Env) -> httpx.Response:
    return await env.units.get("/api/sample/ping")


def test_extension_lifecycle(env):
    async def main():
        write_ext(env, "one")
        report = await pluggables.sync(env.engine, kinds=(pluggables.EXT,))
        assert report.ok and env.engine.generation == 1
        # load -> routes, tools, panel, config overlay, task, loaded set
        assert (await ping(env)).json() == {"word": "one"}
        assert "sample_tool" in toolspec.registered_names()
        assert json.loads(await toolspec.dispatch("sample_tool", {"text": "x"})) == {"word": "one", "text": "x"}
        panel = next(p for p in await env.panels() if p["unit"] == "ext:sample")
        assert panel["kind"] == "script" and panel["script"] == "/extensions/sample/panel.js"
        assert env.engine.effective_config()["backends"]["sample_backend"]["kind"] == "cli"
        assert extensions.loaded().by_name("sample") is not None
        await asyncio.sleep(0.1)
        assert "tick-one" in env.probe_lines()

        # swap to a broken version: old still serving, generation unchanged
        generation = env.engine.generation
        write_ext(env, "two", broken=True)
        response = await env.swap("ext:sample")
        assert response.status_code == 409 and "broken on purpose" in json.dumps(response.json())
        assert env.engine.generation == generation
        assert (await ping(env)).json() == {"word": "one"}
        assert json.loads(await toolspec.dispatch("sample_tool", {}))["word"] == "one"
        assert extensions.loaded().by_name("sample").api.panels

        # swap to a good version: new behaviour, old drained (its task cancelled, one unit left)
        write_ext(env, "two")
        response = await env.swap("ext:sample")
        assert response.status_code == 200, response.text
        await env.engine.settle()
        assert env.engine.generation == generation + 1
        assert (await ping(env)).json() == {"word": "two"}
        assert json.loads(await toolspec.dispatch("sample_tool", {}))["word"] == "two"
        assert "cancelled-one" in env.probe_lines() and "tick-two" in env.probe_lines()
        assert [u["version"] for u in env.engine.status()["units"] if u["unit"] == "ext:sample"] == [
            pluggables.version_of("ext:sample")]
        # an unchanged folder is no new version
        assert (await env.swap("ext:sample")).json()["plan"]["ops"] == []

        # unload: nothing left
        report = await env.engine.unload("ext:sample")
        assert report.ok
        await env.engine.settle()
        gone = await ping(env)
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "unit_unavailable"
        assert "sample_tool" not in toolspec.registered_names()
        assert not [p for p in await env.panels() if p["unit"] == "ext:sample"]
        assert "cancelled-two" in env.probe_lines()
        assert env.engine.supervisor.total() == 0
        assert extensions.loaded().by_name("sample") is None
        assert "sample_backend" not in env.engine.effective_config().get("backends", {})

    run(main())


def test_extension_request_in_flight_during_swap_completes(env):
    async def main():
        write_ext(env, "one")
        await pluggables.sync(env.engine, kinds=(pluggables.EXT,))
        slow = asyncio.create_task(env.units.get("/api/sample/slow"))
        await asyncio.sleep(0.1)                                   # the request is inside the old version
        write_ext(env, "two")
        response = await env.swap("ext:sample")
        assert response.status_code == 200
        assert (await ping(env)).json() == {"word": "two"}          # new requests already see the new version
        finished = await slow
        assert finished.status_code == 200 and finished.json() == {"word": "one"}
        await env.engine.settle()

    run(main())


def test_a_broken_extension_does_not_stop_a_good_one_and_is_reported(env):
    async def main():
        write_ext(env, "one")
        bad = env.tmp / "private" / "extensions" / "bad"
        (bad / "bad_ext").mkdir(parents=True)
        (bad / "abp_extension.toml").write_text(
            '[extension]\nname = "bad"\nversion = "1"\ndescription = "d"\nentry = "bad_ext"\n', encoding="utf-8")
        (bad / "bad_ext" / "__init__.py").write_text("def register(api):\n    raise ValueError('nope')\n")
        await pluggables.sync(env.engine, kinds=(pluggables.EXT,))
        assert (await ping(env)).status_code == 200
        assert "nope" in extensions.loaded().errors["bad"]
        assert "nope" in pluggables.errors["ext:bad@default"]

    run(main())


# ---------------------------------------------------------------------------------------------- python plugins
PLUGIN_SRC = '''\
import asyncio
import os


def _log(message):
    with open(os.environ["SAMPLE_PROBE"], "a") as fh:
        fh.write(message + "\\n")


def setup(api):
    word = "{word}"
    {broken}

    async def handler(tool_input, *, workspace=None, instance_id=None):
        return word

    api.register_tool("sample_plugin_tool", "Say the word.", {{"type": "object", "properties": {{}}}}, handler)

    async def run():
        try:
            while True:
                _log("ptick-" + word)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            _log("pcancelled-" + word)
            raise

    api.spawn(run)


def teardown():
    _log("teardown-{word}")
'''


def write_plugin(env: Env, word: str, broken: bool = False) -> Path:
    path = env.tmp / "sample_plugin.py"
    path.write_text(PLUGIN_SRC.format(word=word, broken='raise RuntimeError("setup broke")' if broken else "pass"),
                    encoding="utf-8")
    return path


async def plugin_word() -> str:
    return await plugins.execute_tool("sample_plugin_tool", {}, workspace=Path("."), instance_id=None)


def test_plugin_lifecycle(env):
    async def main():
        path = write_plugin(env, "one")
        db.install_plugin_row("sample_plugin", str(path), "a sample")
        report = await pluggables.sync(env.engine, kinds=(pluggables.PLUGIN,))
        assert report.ok
        assert plugins.has_tool("sample_plugin_tool") and await plugin_word() == "one"
        await asyncio.sleep(0.1)
        assert "ptick-one" in env.probe_lines()

        generation = env.engine.generation
        write_plugin(env, "two", broken=True)
        response = await env.swap("plugin:sample_plugin")
        assert response.status_code == 409 and "setup broke" in json.dumps(response.json())
        assert env.engine.generation == generation and await plugin_word() == "one"
        assert not any(line.startswith("teardown") for line in env.probe_lines())     # the old one was never touched

        write_plugin(env, "two")
        assert (await env.swap("plugin:sample_plugin")).status_code == 200
        await env.engine.settle()
        assert await plugin_word() == "two" and env.engine.generation == generation + 1
        lines = env.probe_lines()
        assert "teardown-one" in lines and "pcancelled-one" in lines and "teardown-two" not in lines

        assert (await env.engine.unload("plugin:sample_plugin")).ok
        await env.engine.settle()
        assert not plugins.has_tool("sample_plugin_tool")
        assert "teardown-two" in env.probe_lines() and "pcancelled-two" in env.probe_lines()
        assert env.engine.supervisor.total() == 0 and "sample_plugin" not in plugins._loaded

    run(main())


def test_installing_over_a_plugin_is_a_swap_and_a_failed_version_keeps_the_old(env):
    async def main():
        path = write_plugin(env, "one")
        plugins.install(str(path))
        assert await plugin_word() == "one"
        write_plugin(env, "two", broken=True)
        with pytest.raises(plugins.PluginError, match="setup broke"):
            plugins.install(str(path))
        assert await plugin_word() == "one"                         # today's order (unregister first) would lose it
        assert not any(line.startswith("teardown") for line in env.probe_lines())
        write_plugin(env, "two")
        plugins.install(str(path))
        assert await plugin_word() == "two" and "teardown-one" in env.probe_lines()
        await asyncio.sleep(0.05)
        plugins.disable("sample_plugin")
        assert not plugins.has_tool("sample_plugin_tool") and "teardown-two" in env.probe_lines()
        await asyncio.sleep(0.05)
        assert "pcancelled-two" in env.probe_lines()
        plugins.remove("sample_plugin")

    run(main())


# ---------------------------------------------------------------------------------------------- wasm plugins
class FakeHandle:
    handles: list = []

    def __init__(self, word: str) -> None:
        self.word, self.closed = word, False
        self.manifest = {"name": "samplewasm", "tools": [{"name": "echo", "description": "echo", "schema": ""}]}
        FakeHandle.handles.append(self)

    def invoke(self, tool, args, timeout_ms=0):
        assert not self.closed
        return {"ok": json.dumps({"word": self.word})}

    def close(self):
        self.closed = True


class FakeEngine:
    def load(self, path, *, services, grants, memory_limit_bytes):
        from bot.wasm_plugins import runtime as rt

        text = Path(path).read_text()
        if text == "BROKEN":
            raise rt.PluginCallError("cannot compile")
        return rt.LoadedPlugin(FakeHandle(text))


@pytest.fixture
def wasm(env, monkeypatch):
    from bot.wasm_plugins import manager
    from bot.wasm_plugins import runtime as rt

    FakeHandle.handles = []
    root = env.tmp / "wasm"
    monkeypatch.setattr(manager, "_root", lambda: root)
    monkeypatch.setattr(rt.Runtime, "shared", classmethod(lambda cls: FakeEngine()))
    manager._loaded.clear()
    component = root / "samplewasm" / "plugin.wasm"
    component.parent.mkdir(parents=True)
    manager._save_state({"plugins": {"samplewasm": {
        "name": "samplewasm", "version": "1", "description": "d", "component": str(component), "enabled": True,
        "grants": {"preopens": [], "network": []}, "timeout_ms": 1000, "memory_mb": 16}}})
    yield SimpleNamespace(component=component, manager=manager, rt=rt)
    manager._loaded.clear()
    from bot.wasm_plugins import tools as wasm_tools

    wasm_tools.reset_for_tests()


def test_wasm_lifecycle(env, wasm):
    async def call() -> str:
        return json.loads(await toolspec.dispatch("samplewasm_echo", {}))["word"]

    async def main():
        wasm.component.write_text("one")
        assert (await pluggables.sync(env.engine, kinds=(pluggables.WASM,))).ok
        assert "samplewasm_echo" in toolspec.registered_names() and await call() == "one"
        assert wasm.manager.is_loaded("samplewasm")

        generation = env.engine.generation
        wasm.component.write_text("BROKEN")
        response = await env.swap("wasm:samplewasm")
        assert response.status_code == 409 and "cannot compile" in json.dumps(response.json())
        assert env.engine.generation == generation and await call() == "one"
        assert not FakeHandle.handles[0].closed

        wasm.component.write_text("two")
        assert (await env.swap("wasm:samplewasm")).status_code == 200
        await env.engine.settle()
        assert await call() == "two" and env.engine.generation == generation + 1
        assert FakeHandle.handles[0].closed and not FakeHandle.handles[-1].closed

        assert (await env.engine.unload("wasm:samplewasm")).ok
        await env.engine.settle()
        assert "samplewasm_echo" not in toolspec.registered_names() and not wasm.manager.is_loaded("samplewasm")
        assert all(h.closed for h in FakeHandle.handles)

    run(main())


def test_direct_enable_of_a_wasm_plugin_keeps_the_old_one_when_the_new_will_not_load(env, wasm):
    wasm.component.write_text("one")
    wasm.manager.enable("samplewasm")
    first = FakeHandle.handles[0]
    wasm.component.write_text("BROKEN")
    with pytest.raises(wasm.manager.WasmPluginError):
        wasm.manager.enable("samplewasm")
    assert wasm.manager.is_loaded("samplewasm") and not first.closed and "samplewasm_echo" in toolspec.registered_names()
    wasm.component.write_text("two")
    wasm.manager.enable("samplewasm")
    assert first.closed and json.loads(wasm.manager.invoke("samplewasm", "echo", {}))["word"] == "two"
    wasm.manager.disable("samplewasm")
    assert "samplewasm_echo" not in toolspec.registered_names() and FakeHandle.handles[-1].closed


def test_wasm_units_report_failed_without_the_native_module(env, wasm, monkeypatch):
    async def main():
        write_ext(env, "one")
        wasm.component.write_text("one")

        def unavailable(cls):
            raise wasm.rt.WasmUnavailable("the native module is not built (test)")

        monkeypatch.setattr(wasm.rt.Runtime, "shared", classmethod(unavailable))
        await pluggables.sync(env.engine)
        assert "not built" in pluggables.errors["wasm:samplewasm@default"]
        assert (await ping(env)).json() == {"word": "one"}           # nothing else broke
        assert "samplewasm_echo" not in toolspec.registered_names()

    run(main())


# ---------------------------------------------------------------------------------------------- external MCP servers
MCP_SERVER = '''\
import sys
from mcp.server.mcpserver import MCPServer

word = sys.argv[1]
server = MCPServer("sample")


@server.tool()
def shout(text: str) -> str:
    """Shout the text."""
    return word + ":" + text


server.run()
'''


def add_mcp(env: Env, word: str, broken: bool = False) -> None:
    script = env.tmp / "mcp_sample.py"
    script.write_text(MCP_SERVER, encoding="utf-8")
    cmd_args = [str(script), word] if not broken else ["-c", "import sys; sys.exit(3)"]
    conn = db.get_conn()
    with db._lock:
        conn.execute("DELETE FROM external_mcp_servers WHERE name='sample'")
        conn.commit()
    db.add_external_mcp_server("sample", "stdio", command=sys.executable, args_json=json.dumps(cmd_args))


def test_mcp_lifecycle(env):
    async def call() -> str:
        return await toolspec.dispatch("mcp_sample_shout", {"text": "x"})

    async def main():
        add_mcp(env, "one")
        report = await pluggables.sync(env.engine, kinds=(pluggables.MCP,))
        assert report.ok, report.to_dict()
        assert await call() == "one:x" and "sample" in mcp_client.runtime_owned
        assert "mcp_sample_shout" in [s["name"] for s in toolspec.registered_schemas()]
        assert "sample" in mcp_client.connected_servers() and not mcp_client._connections   # one table: the unit's

        generation = env.engine.generation
        add_mcp(env, "x", broken=True)
        response = await env.swap("mcp:sample")
        assert response.status_code == 409
        assert env.engine.generation == generation and await call() == "one:x"

        add_mcp(env, "two")
        assert (await env.swap("mcp:sample")).status_code == 200
        await env.engine.settle()
        assert await call() == "two:x" and env.engine.generation == generation + 1

        assert (await env.engine.unload("mcp:sample")).ok
        await env.engine.settle()
        assert "mcp_sample_shout" not in toolspec.registered_names()
        assert "sample" not in mcp_client.runtime_owned and not pluggables._owners

    run(main())


def test_legacy_connect_all_skips_servers_a_unit_owns(env, monkeypatch):
    async def main():
        add_mcp(env, "one")
        await pluggables.sync(env.engine, kinds=(pluggables.MCP,))
        called = []

        async def fake_connect(name, **kw):
            called.append(name)
            return True

        monkeypatch.setattr(mcp_client, "connect", fake_connect)
        await mcp_client.connect_all_enabled()
        assert called == []
        await env.engine.unload("mcp:sample")
        await env.engine.settle()

    run(main())


# ---------------------------------------------------------------------------------------------- the dashboard app
def test_extension_routes_through_the_real_app_and_the_old_listing(env, monkeypatch):
    from bot.dashboard.server import build_app
    from bot.runtime import live

    monkeypatch.setenv("DASHBOARD_TOKEN", "t")
    monkeypatch.delenv("ABP_UNITS", raising=False)
    live.reset()
    write_ext(env, "one")
    app = build_app()
    headers = {"X-Dashboard-Token": "t"}

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/sample/ping", headers=headers)).json() == {"word": "one"}
            assert (await c.get("/api/sample/ping", headers={"X-Dashboard-Token": "nope"})).status_code in (401, 403)
            listing = (await c.get("/api/extensions", headers=headers)).json()
            assert [e["name"] for e in listing["extensions"]] == ["sample"]
            assert listing["panels"][0]["script"] == "/extensions/sample/panel.js"
            write_ext(env, "two")
            swapped = await c.post("/api/runtime/units/ext:sample/swap", headers=headers, json={})
            assert swapped.status_code == 200, swapped.text
            assert (await c.get("/api/sample/ping", headers=headers)).json() == {"word": "two"}
            panels = (await c.get("/api/ui/manifest", headers=headers)).json()["panels"]
            assert any(p["unit"] == "ext:sample" and p["kind"] == "script" for p in panels)
            unloaded = await c.post("/api/runtime/units/ext:sample/unload", headers=headers, json={})
            assert unloaded.status_code == 200
            gone = await c.get("/api/sample/ping", headers=headers)
            assert gone.status_code == 404 and gone.json()["error"]["code"] == "unit_unavailable"
        await live.shutdown()

    run(main())
    live.reset()


# ---------------------------------------------------------------------------------------------- dashboard routes are transactions
def _events(c, headers, since=0):
    return c.get(f"/api/runtime/events?since={since}", headers=headers).json()


def _unit_ids(c, headers) -> set:
    return {u["unit"] for u in c.get("/api/runtime", headers=headers).json()["units"]}


def test_plugin_routes_are_engine_transactions(env, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app
    from bot.runtime import live

    monkeypatch.setenv("DASHBOARD_TOKEN", "t")
    monkeypatch.setenv("ABP_UNITS", "none")
    live.reset()
    path = write_plugin(env, "one")
    h = {"X-Dashboard-Token": "t"}
    with TestClient(build_app()) as c:
        g0 = c.get("/api/runtime", headers=h).json()["generation"]
        assert c.post("/api/plugins", headers=h, json={"path": str(path)}).status_code == 200
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 1
        assert "plugin:sample_plugin" in _unit_ids(c, h)
        assert "plugin:sample_plugin@default" in _events(c, h, g0)["events"][-1]["added"]
        assert plugins.has_tool("sample_plugin_tool")

        assert c.post("/api/plugins/sample_plugin/disable", headers=h).status_code == 200
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 2
        assert "plugin:sample_plugin" not in _unit_ids(c, h)
        assert "plugin:sample_plugin@default" in _events(c, h, g0 + 1)["events"][-1]["removed"]
        assert not plugins.has_tool("sample_plugin_tool")

        assert c.post("/api/plugins/sample_plugin/enable", headers=h).status_code == 200
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 3 and plugins.has_tool("sample_plugin_tool")
        assert c.delete("/api/plugins/sample_plugin", headers=h).status_code == 200
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 4
        assert not plugins.has_tool("sample_plugin_tool")
    live.reset()


def test_mcp_routes_are_engine_transactions_with_one_connection_table(env, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app
    from bot.runtime import live

    monkeypatch.setenv("DASHBOARD_TOKEN", "t")
    monkeypatch.setenv("ABP_UNITS", "none")
    live.reset()
    script = env.tmp / "mcp_sample.py"
    script.write_text(MCP_SERVER, encoding="utf-8")
    h = {"X-Dashboard-Token": "t"}
    with TestClient(build_app()) as c:
        g0 = c.get("/api/runtime", headers=h).json()["generation"]
        r = c.post("/api/mcp-external", headers=h, json={"name": "sample", "transport": "stdio",
                                                          "command": sys.executable, "args": [str(script), "one"]})
        assert r.status_code == 200 and r.json()["connected"] is True
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 1
        assert "mcp:sample@default" in _events(c, h, g0)["events"][-1]["added"]
        assert "mcp_sample_shout" in toolspec.registered_names() and not mcp_client._connections
        assert c.get("/api/mcp-external", headers=h).json()["servers"][0]["connected"] is True

        assert c.post("/api/mcp-external/sample/disable", headers=h).status_code == 200
        assert c.get("/api/runtime", headers=h).json()["generation"] == g0 + 2
        assert "mcp:sample@default" in _events(c, h, g0 + 1)["events"][-1]["removed"]
        assert "mcp_sample_shout" not in toolspec.registered_names()
        assert c.get("/api/mcp-external", headers=h).json()["servers"][0]["connected"] is False

        assert c.post("/api/mcp-external/sample/enable", headers=h).json()["connected"] is True
        assert c.delete("/api/mcp-external/sample", headers=h).status_code == 200
        assert "mcp_sample_shout" not in toolspec.registered_names()
    live.reset()


def test_mcp_timeout_names_its_cause(tmp_path):
    """A server that never answers: the error says how long, where the client was stuck, and what the server wrote to stderr."""
    script = tmp_path / "silent.py"
    script.write_text("import sys, time\nsys.stderr.write('importing the world\\n'); sys.stderr.flush()\ntime.sleep(60)\n", encoding="utf-8")
    hung = {"transport": "stdio", "command": sys.executable, "args": [str(script)], "env": None, "url": None, "auth_token": None}

    async def main():
        with pytest.raises(TimeoutError) as caught:
            await mcp_client.open_private("silent", hung, wait_s=3)
        msg = str(caught.value)
        assert "did not answer within 3s" in msg and "waited " in msg
        assert "initialize" in msg and "importing the world" in msg

    run(main())


def test_mcp_failed_start_includes_server_stderr(tmp_path):
    script = tmp_path / "crash.py"
    script.write_text("import sys\nsys.stderr.write('Traceback: boom\\n')\nsys.exit(3)\n", encoding="utf-8")
    bad = {"transport": "stdio", "command": sys.executable, "args": [str(script)], "env": None, "url": None, "auth_token": None}

    async def main():
        with pytest.raises(RuntimeError) as caught:
            await mcp_client.open_private("crash", bad, wait_s=20)
        assert "boom" in str(caught.value)

    run(main())
