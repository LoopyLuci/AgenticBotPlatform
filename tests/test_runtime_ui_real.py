"""Guarantee 9 in a real browser, against the REAL dashboard page served by the real app (tests/runtime_realapp.py):
the navigation and the sections follow the units that are live, a moment after they change, over the websocket event
and over the polling fallback, with no console errors. The desktop app's UI (desktop-app/ui/index.html, served by the
same app at /desktop-ui/ exactly as in a plain browser) is checked the same way. Skipped when Playwright or an
installed Edge/Chrome is not available."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.xdist_group("real-browser")
pytest.importorskip("playwright")

from runtime_realapp import real_app  # noqa: E402,F401  (real_app is a fixture)

from bot.runtime import legacy  # noqa: E402

PAGES = {"dashboard": "/", "desktop": "/desktop-ui/?booted=1"}
UNITS_WITH_PAGE = [u for u, a in legacy.AREAS.items() if a.panel]
NAV = '#sidenav a[data-unit="{}"]'
EXT_TOML = ('[extension]\nname = "sample"\nversion = "1.0.0"\ndescription = "A neutral sample extension."\n'
            'entry = "sample_ext"\n')
EXT_ENTRY = 'def register(api):\n    api.add_panel("sample", "Sample")\n'
EXT_PANEL = """
ABP.panels.define('sample', {
  mount: function (root, ctx) { root.textContent = 'sample panel mounted'; },
  unmount: function () {}
});
"""


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        for channel in ("msedge", "chrome", ""):
            try:
                b = p.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                break
            except Exception:  # noqa: BLE001 - try the next browser
                continue
        else:
            pytest.skip("no Edge, Chrome or Playwright Chromium available")
        yield b
        b.close()


# Resource 404s that do not depend on any unit (present with every unit and with none): the page asks for a
# favicon the app does not serve, and for the Android builds folder, which a fresh temporary data folder lacks.
IGNORED_404 = ("/favicon.ico", "/api/files/android-builds")


def _console(msg, errors: list) -> None:
    if msg.type != "error":
        return
    url = (msg.location or {}).get("url", "")
    if "status of 404" in msg.text and any(part in url for part in IGNORED_404):
        return
    errors.append(f"console.error: {msg.text} [{url}]")


def _open(browser, app, which: str, errors: list):
    page = browser.new_page(bypass_csp=True)      # Playwright evaluates its waits as strings
    page.on("pageerror", lambda exc: errors.append("pageerror: " + str(exc)))
    page.on("console", lambda m: _console(m, errors))
    page.goto(app.url + PAGES[which], wait_until="load")
    page.wait_for_function("window.ABP && ABP.moduleHost && ABP.moduleHost.available === true", timeout=30_000)
    return page


def _shown(page, unit: str) -> bool:
    return page.locator(NAV.format(unit)).is_visible()


def _wait_shown(page, unit: str, shown: bool, timeout: int = 1500) -> None:
    """The nav entry reaches the wanted state within `timeout` ms (the websocket path must be this fast)."""
    page.wait_for_function(
        "([sel, want]) => { const e = document.querySelector(sel); "
        "return !!e && ((getComputedStyle(e).display !== 'none') === want); }",
        arg=[NAV.format(unit), shown], timeout=timeout)


SOCKET_UP = ("(typeof liveEventsSocket !== 'undefined' && !!liveEventsSocket && liveEventsSocket.readyState === 1)"
             " || (typeof devicesSocket !== 'undefined' && !!devicesSocket && devicesSocket.readyState === 1)")


def _wait_socket(page) -> None:
    page.wait_for_function(SOCKET_UP, timeout=15_000)


def _quiet(page, ms: int = 1500) -> None:
    page.wait_for_timeout(ms)


@pytest.mark.parametrize("which", list(PAGES))
def test_with_all_units_every_area_nav_entry_is_visible_without_console_errors(real_app, browser, which):
    app = real_app("all")
    errors: list = []
    page = _open(browser, app, which, errors)
    for unit in UNITS_WITH_PAGE:
        assert page.locator(NAV.format(unit)).count() == 1, unit
        _wait_shown(page, unit, True, timeout=5_000)
        assert _shown(page, unit), unit
    assert page.locator("#sidenav [data-unit-hidden]").count() == 0
    _quiet(page)
    assert not errors, errors


@pytest.mark.parametrize("which", list(PAGES))
def test_with_no_units_no_area_is_shown_and_the_core_pages_work(real_app, browser, which):
    app = real_app("none")
    errors: list = []
    page = _open(browser, app, which, errors)
    for unit in UNITS_WITH_PAGE:
        _wait_shown(page, unit, False, timeout=5_000)
        assert not _shown(page, unit), unit
        assert not page.locator(f'section[data-unit="{unit}"]').is_visible(), unit
    for core in ("overview", "chat", "models", "logs"):
        link = page.locator(f'#sidenav a[href="#{core}"]')
        assert link.is_visible(), core
        link.click()
        page.wait_for_function(f"location.hash === '#{core}'", timeout=5_000)
        assert page.locator(f"section#{core}").count() == 1
    _quiet(page)
    assert not errors, errors


@pytest.mark.parametrize("which", list(PAGES))
def test_unloading_and_loading_an_area_updates_the_page_within_a_second(real_app, browser, which):
    app = real_app("all")
    errors: list = []
    page = _open(browser, app, which, errors)
    _wait_socket(page)
    unit = "ollama"
    assert _shown(page, unit) and _shown(page, "vm-harness")
    assert app.unload(unit).json()["ok"]
    _wait_shown(page, unit, False)                       # 1.5 s budget over the websocket event
    assert not page.locator(f'section[data-unit="{unit}"]').is_visible()
    assert _shown(page, "vm-harness"), "other areas stay"
    assert app.load(unit).json()["ok"]
    _wait_shown(page, unit, True)
    _quiet(page, 800)
    assert not errors, errors


@pytest.mark.parametrize("which", list(PAGES))
def test_the_polling_fallback_updates_the_page_when_the_socket_is_closed(real_app, browser, which):
    app = real_app("all")
    errors: list = []
    page = _open(browser, app, which, errors)
    _wait_socket(page)
    # close the socket from page JS and stop it reconnecting: only the 5 s poll is left
    closed = page.evaluate("""() => {
        const names = ['connectLiveEventsSocket', 'connectDevicesSocket'];
        for (const n of names) { try { if (typeof window[n] === 'function') window[n] = function () {}; } catch (e) {} }
        let sock = null;
        try { sock = typeof liveEventsSocket !== 'undefined' ? liveEventsSocket : null; } catch (e) {}
        if (!sock) { try { sock = typeof devicesSocket !== 'undefined' ? devicesSocket : null; } catch (e) {} }
        if (sock) sock.close();
        return !!sock;
    }""")
    assert closed, "the page keeps its websocket in a global the test can reach"
    page.wait_for_function("!(" + SOCKET_UP + ")", timeout=5_000)
    assert app.unload("hosting").json()["ok"]
    _wait_shown(page, "hosting", False, timeout=12_000)
    assert not page.evaluate(SOCKET_UP), "it was the poll, not a reconnected socket"
    assert app.load("hosting").json()["ok"]
    _wait_shown(page, "hosting", True, timeout=12_000)
    assert not errors, errors


@pytest.mark.parametrize("which", list(PAGES))
def test_an_open_area_page_whose_unit_is_unloaded_says_so(real_app, browser, which):
    app = real_app("all")
    errors: list = []
    page = _open(browser, app, which, errors)
    _wait_socket(page)
    page.locator(NAV.format("hosting")).click()
    page.wait_for_function("location.hash === '#hosting'", timeout=5_000)
    assert app.unload("hosting").json()["ok"]
    page.wait_for_function(
        "document.body.textContent.indexOf('This module was unloaded') >= 0", timeout=3_000)
    assert not page.locator(NAV.format("hosting")).is_visible()
    assert app.load("hosting").json()["ok"]
    _wait_shown(page, "hosting", True)
    page.wait_for_function("document.querySelector('[data-module-unloaded-note]') === null", timeout=3_000)
    assert not errors, errors


@pytest.mark.parametrize("which", list(PAGES))
def test_a_script_panel_of_an_extension_follows_its_unit(real_app, browser, which, tmp_path):
    ext = tmp_path / "private" / "extensions" / "sample"
    (ext / "sample_ext").mkdir(parents=True)
    (ext / "abp_extension.toml").write_text(EXT_TOML, encoding="utf-8")
    (ext / "sample_ext" / "__init__.py").write_text(EXT_ENTRY, encoding="utf-8")
    (ext / "panel.js").write_text(EXT_PANEL, encoding="utf-8")
    app = real_app("all")
    assert "ext:sample" in app.units()
    errors: list = []
    page = _open(browser, app, which, errors)
    _wait_socket(page)
    page.wait_for_selector("section#sample", state="attached", timeout=5_000)
    page.wait_for_function("document.getElementById('sample-root') && "
                           "document.getElementById('sample-root').textContent.indexOf('mounted') >= 0", timeout=5_000)
    assert page.locator('#sidenav a[href="#sample"]').count() == 1
    assert app.unload("ext:sample").json()["ok"]
    page.wait_for_function("document.querySelector('section#sample') === null", timeout=1_500)
    assert page.locator('#sidenav a[href="#sample"]').count() == 0
    assert page.locator('script[src*="/extensions/sample/panel.js"]').count() == 0
    assert app.load("ext:sample").json()["ok"]
    page.wait_for_selector("section#sample", state="attached", timeout=1_500)
    page.wait_for_function("document.getElementById('sample-root') && "
                           "document.getElementById('sample-root').textContent.indexOf('mounted') >= 0", timeout=5_000)
    assert page.locator('#sidenav a[href="#sample"]').count() == 1
    _quiet(page, 800)
    assert not errors, errors
