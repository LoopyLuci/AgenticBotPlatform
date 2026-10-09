"""The front end's module host (module-host.js): nav and sections follow the runtime's UI manifest.

The real-browser tests serve a tiny stand-in for the backend (a fake /api/ui/manifest and a websocket that pushes
runtime.changed) next to the real module-host.js, so they run without the unit runtime. Skipped when Playwright or an
installed Edge/Chrome is not available."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from fastapi import FastAPI, WebSocket  # noqa: E402  (module level: FastAPI resolves string annotations here)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "bot" / "dashboard" / "static"
DESKTOP = ROOT / "desktop-app" / "ui"

# data-unit ids chosen for the hard-coded optional pages: page section id -> unit id
UNIT_OF_SECTION = {
    "router": "router", "unsloth": "unsloth", "studio": "studio", "localai": "localai", "lab": "neurallab",
    "storage": "storage", "hosting": "hosting", "vision": "vision", "private_project": "private_project", "cluster": "cluster",
    "modules": "modules", "transferdaemon": "transferdaemon", "power": "power", "vm-harness": "vm-harness",
    "hermes-manager": "hermes-manager", "ollama": "ollama",
}


# ------------------------------------------------------------------ files (no browser needed)
def test_module_host_is_identical_in_both_front_ends():
    a = (WEB / "module-host.js").read_text(encoding="utf-8")
    b = (DESKTOP / "module-host.js").read_text(encoding="utf-8")
    assert a == b, "the dashboard and the desktop app must ship the same module-host.js"
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert "module-host.js" in html
        assert html.index("module-host.js") < html.index("extensions.js"), "the host loads before the fallback"


def test_every_shared_panel_file_is_identical_in_both_front_ends():
    for f in sorted(WEB.glob("*.js")):
        if f.name == "dashboard.js":
            continue
        other = DESKTOP / f.name
        assert other.exists(), f.name
        assert f.read_bytes() == other.read_bytes(), f"{f.name} differs between the dashboard and the desktop app"


@pytest.mark.parametrize("page", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"])
def test_optional_pages_carry_data_unit_and_core_pages_do_not(page):
    html = (ROOT / page).read_text(encoding="utf-8")
    for section, unit in UNIT_OF_SECTION.items():
        assert f'<section id="{section}" data-unit="{unit}">' in html, section
        assert re.search(rf'<a href="#{re.escape(section)}" data-unit="{re.escape(unit)}"', html), section
    for core in ("overview", "chat", "bots", "models", "logs"):
        assert f'<section id="{core}" data-unit' not in html, core


def test_front_end_hooks_exist_in_both_apps():
    for js in (WEB / "dashboard.js", DESKTOP / "main.js"):
        text = js.read_text(encoding="utf-8")
        for needle in ("runtime.changed", "ABP.moduleHost.onEvent", "ABP.moduleHost.socketUp",
                       "__abpWireNavLink", "__abpObserveSection"):
            assert needle in text, (js.name, needle)


def test_module_panels_say_module_not_loaded_on_unit_unavailable():
    for d in (WEB, DESKTOP):
        for f in d.glob("*-panel.js"):
            text = f.read_text(encoding="utf-8")
            if "const errText" in text:
                assert "ABP.unitUnavailable" in text, f.name
    assert "unit_unavailable" in (WEB / "ui-prelude.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------ real browser
HARNESS = """<!doctype html><html><head><meta charset="utf-8"><title>harness</title></head><body>
<nav id="sidenav">
  <a href="#overview">Overview</a>
  <a href="#vm-harness" data-unit="vm-harness">VM-Harness</a>
  <a href="#hosting" data-unit="hosting" style="display:flex">Hosting</a>
</nav>
<main>
  <section id="overview">Overview</section>
  <section id="vm-harness" data-unit="vm-harness">VM</section>
  <section id="hosting" data-unit="hosting">Hosting</section>
</main>
<script src="/static/module-host.js"></script>
<script>
(function () {
  if (location.search.indexOf('nosocket') >= 0) return;
  function connect() {
    var ws = new WebSocket('ws://' + location.host + '/api/ws');
    ws.onopen = function () { ABP.moduleHost.socketUp(true); };
    ws.onmessage = function (e) { ABP.moduleHost.onEvent(JSON.parse(e.data)); };
    ws.onclose = function () { ABP.moduleHost.socketUp(false); setTimeout(connect, 500); };
  }
  connect();
})();
</script></body></html>"""

PANEL_JS = """
ABP.panels.define('example-ext', {
  mount: function (root, ctx) {
    root.textContent = 'mounted ' + (ctx.version || '');
    window.__mounts = (window.__mounts || 0) + 1;
    ctx.every(40, function () { window.__ticks = (window.__ticks || 0) + 1; });
    ctx.on(window, 'resize', function () {});
  },
  unmount: function () { window.__unmounts = (window.__unmounts || 0) + 1; }
});
"""

LEGACY_JS = """
(function () {
  var root = document.getElementById('old-ext-root');
  root.textContent = 'legacy ready';
})();
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBackend:
    def __init__(self, port: int):
        self.port = port
        self.panels: list[dict] = []
        self.generation = 1
        self.manifest_status = 200
        self.sockets: list = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.script_urls: list[str] = []

    def set_panels(self, panels, push=True):
        self.panels = panels
        self.generation += 1
        if push:
            self.push({"type": "runtime.changed", "generation": self.generation, "added": [], "removed": [], "changed": []})

    def push(self, msg):
        async def go():
            for ws in list(self.sockets):
                try:
                    await ws.send_text(json.dumps(msg))
                except Exception:  # noqa: BLE001
                    pass
        if self.loop:
            asyncio.run_coroutine_threadsafe(go(), self.loop).result(timeout=5)

    def drop_sockets(self):
        async def go():
            for ws in list(self.sockets):
                await ws.close()
            self.sockets.clear()
        if self.loop:
            asyncio.run_coroutine_threadsafe(go(), self.loop).result(timeout=5)


@pytest.fixture
def backend():
    pytest.importorskip("playwright")
    import uvicorn

    state = FakeBackend(_free_port())
    app = FastAPI()

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/")
    def index():
        return HTMLResponse(HARNESS)

    @app.get("/static/module-host.js")
    def host_js():
        return Response((WEB / "module-host.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/ext/panel.js")
    def panel_js(v: str = ""):
        state.script_urls.append(f"/ext/panel.js?v={v}")
        return Response(PANEL_JS, media_type="text/javascript")

    @app.get("/ext/legacy.js")
    def legacy_js():
        return Response(LEGACY_JS, media_type="text/javascript")

    @app.get("/api/ui/manifest")
    def manifest():
        if state.manifest_status == 404:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return {"generation": state.generation, "panels": state.panels}

    @app.get("/api/runtime/events")
    def events(since: int = 0):
        return {"generation": state.generation, "events": []}

    @app.get("/api/boom")
    def boom():
        return PlainTextResponse("no", status_code=500)

    @app.websocket("/api/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        state.loop = asyncio.get_running_loop()
        state.sockets.append(sock)
        try:
            while True:
                await sock.receive_text()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if sock in state.sockets:
                state.sockets.remove(sock)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=state.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    state.url = f"http://127.0.0.1:{state.port}"
    yield state
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        for channel in ("msedge", "chrome", ""):
            try:
                b = p.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            pytest.skip("no Edge, Chrome or Playwright Chromium available")
        yield b
        b.close()


def _open(browser, url, errors):
    page = browser.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url, wait_until="load")
    return page


def _visible(page, selector):
    return page.locator(selector).is_visible()


def _builtin(unit, section=None):
    return {"id": unit, "unit": unit, "instance": "default", "version": "v-1", "title": unit, "group": "modules",
            "icon": "x", "order": 1, "kind": "builtin", "section": section or unit}


def _script(version="v-1", pid="example-ext", script="/ext/panel.js"):
    return {"id": pid, "unit": "ext:" + pid, "instance": "default", "version": version, "title": "Example Ext",
            "group": "extensions", "icon": "E", "order": 100, "kind": "script", "script": script, "styles": []}


@pytest.mark.xdist_group("real-browser")
def test_zero_panels_hide_every_optional_link_without_console_errors(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    assert not _visible(page, 'a[data-unit="vm-harness"]')
    assert not _visible(page, 'a[data-unit="hosting"]'), "a link with its own display style is hidden too"
    assert not _visible(page, 'section[data-unit="vm-harness"]')
    assert _visible(page, 'a[href="#overview"]'), "core pages have no data-unit and stay"
    assert not errors, errors


@pytest.mark.xdist_group("real-browser")
def test_a_builtin_panel_toggles_its_nav_and_section(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_builtin("vm-harness")])
    page.wait_for_function("document.querySelector('a[data-unit=\"vm-harness\"]').style.display !== 'none'", timeout=5_000)
    assert _visible(page, 'a[data-unit="vm-harness"]')
    assert _visible(page, 'section[data-unit="vm-harness"]')
    assert not _visible(page, 'a[data-unit="hosting"]')
    backend.set_panels([])
    page.wait_for_function("document.querySelector('a[data-unit=\"vm-harness\"]').style.display === 'none'", timeout=5_000)
    assert not errors, errors


@pytest.mark.xdist_group("real-browser")
def test_a_script_panel_is_created_mounted_versioned_and_cleaned_up(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_script("v-1")])
    page.wait_for_selector("section#example-ext", timeout=5_000)
    page.wait_for_function("window.__mounts === 1", timeout=5_000)
    assert "mounted v-1" in page.locator("#example-ext-root").text_content()
    assert page.locator('#sidenav a[href="#example-ext"]').count() == 1
    assert "/ext/panel.js?v=v-1" in backend.script_urls
    page.wait_for_function("window.__ticks > 2", timeout=5_000)
    dbg = page.evaluate("ABP.moduleHost.debug()")
    assert dbg["timers"] == 1 and dbg["listeners"] == 1

    backend.set_panels([_script("v-2")])      # a new version: unmount the old one, load the new with ?v=
    page.wait_for_function("window.__mounts === 2", timeout=5_000)
    assert page.evaluate("window.__unmounts") == 1
    assert "/ext/panel.js?v=v-2" in backend.script_urls
    assert page.evaluate("ABP.moduleHost.debug().timers") == 1, "the old version's timer is gone"
    assert page.locator("section#example-ext").count() == 1

    backend.set_panels([])
    page.wait_for_function("document.querySelector('section#example-ext') === null", timeout=5_000)
    assert page.locator('#sidenav a[href="#example-ext"]').count() == 0
    dbg = page.evaluate("ABP.moduleHost.debug()")
    assert dbg["timers"] == 0 and dbg["listeners"] == 0 and dbg["panels"] == []
    assert page.evaluate("window.__unmounts") == 2
    before = page.evaluate("window.__ticks")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__ticks") == before, "no timer outlives the panel"
    assert not errors, errors


@pytest.mark.xdist_group("real-browser")
def test_a_legacy_panel_is_wrapped_and_removed_with_its_section_and_script(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_script("v-1", pid="old-ext", script="/ext/legacy.js")])
    page.wait_for_function("document.getElementById('old-ext-root') && document.getElementById('old-ext-root').textContent === 'legacy ready'", timeout=5_000)
    assert page.evaluate("ABP.moduleHost.debug().panels[0].legacy") is True
    assert page.locator('script[src*="/ext/legacy.js"]').count() == 1
    backend.set_panels([])
    page.wait_for_function("document.getElementById('old-ext') === null", timeout=5_000)
    assert page.locator('script[src*="/ext/legacy.js"]').count() == 0
    assert not errors, errors


@pytest.mark.xdist_group("real-browser")
def test_a_script_that_fails_to_load_shows_its_error(backend, browser):
    page = _open(browser, backend.url + "/", [])
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_script("v-1", pid="gone-ext", script="/ext/missing.js")])
    page.wait_for_function("document.getElementById('gone-ext-root') && document.getElementById('gone-ext-root').textContent.indexOf('could not be loaded') >= 0", timeout=5_000)


@pytest.mark.xdist_group("real-browser")
def test_an_open_panel_whose_unit_disappears_says_it_was_unloaded(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_script("v-1")])
    page.wait_for_selector("section#example-ext", timeout=5_000)
    page.evaluate("location.hash = '#example-ext'")
    backend.set_panels([])
    page.wait_for_function("document.querySelector('#example-ext [data-module-note]')", timeout=5_000)
    assert "was unloaded" in page.locator("#example-ext").text_content()
    assert page.evaluate("ABP.moduleHost.debug().timers") == 0


@pytest.mark.xdist_group("real-browser")
def test_a_404_manifest_leaves_the_page_as_it_is(backend, browser):
    backend.manifest_status = 404
    page = _open(browser, backend.url + "/", [])
    page.wait_for_function("ABP.moduleHost.available === false", timeout=10_000)
    for sel in ('a[data-unit="vm-harness"]', 'a[data-unit="hosting"]', 'section[data-unit="vm-harness"]', 'a[href="#overview"]'):
        assert _visible(page, sel), sel
    assert page.locator("[data-unit-hidden]").count() == 0


@pytest.mark.xdist_group("real-browser")
def test_without_a_socket_the_host_polls_for_changes(backend, browser):
    errors: list[str] = []
    page = _open(browser, backend.url + "/?nosocket", errors)
    page.wait_for_function("ABP.moduleHost.available === true", timeout=10_000)
    backend.set_panels([_builtin("hosting")], push=False)
    page.wait_for_function("document.querySelector('a[data-unit=\"hosting\"]').style.display !== 'none'", timeout=12_000)
    assert not errors, errors
