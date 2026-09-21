"""Both UIs' scripts must run to the end in a real browser.

A page script that throws while loading stops there, and everything below it never runs. That is exactly how the desktop
boot code (at the very bottom of main.js) was killed once: a top-level call read a `let` declared further down
("Cannot access ... before initialization"), and the window sat on "Starting the bot process…" forever. Static checks
can't see that, so this loads the real pages in a real browser and fails on any uncaught script error.

Skipped when Playwright or an installed Edge/Chrome is not available.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("playwright")

import uvicorn  # noqa: E402

from bot.dashboard.server import build_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served_dashboard(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the test dashboard did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


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


def _open(browser, url):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(url, wait_until="load")
    page.wait_for_timeout(2500)  # let the load-time code and first polls run
    return page, errors


@pytest.mark.parametrize("path", ["/", "/desktop-ui/"])
def test_the_ui_script_throws_nothing_while_loading(served_dashboard, browser, path):
    page, errors = _open(browser, served_dashboard + path)
    assert errors == [], errors


@pytest.mark.parametrize("path", ["/", "/desktop-ui/"])
def test_chat_opens_in_chat_with_bot_in_the_real_page(served_dashboard, browser, path):
    page, errors = _open(browser, served_dashboard + path)
    assert errors == [], errors
    assert page.evaluate("chatState.mode") == "bot"
    assert page.evaluate("document.getElementById('chat-mode-switch-text').textContent") == "💬 Chat with Bot"
    assert page.evaluate("document.getElementById('chat-card').classList.contains('mode-bot')") is True
    assert page.evaluate("document.getElementById('chat-recipient').classList.contains('hidden')") is True


def test_the_desktop_script_runs_to_its_last_line(served_dashboard, browser):
    """Outside Tauri the very last thing main.js does is hide the boot panel and check setup, so if that has
    happened, nothing above it threw."""
    page, errors = _open(browser, served_dashboard + "/desktop-ui/")
    assert errors == [], errors
    assert page.evaluate("document.getElementById('boot').classList.contains('hidden')") is True


def test_the_top_bar_pill_is_rendered_by_the_script(served_dashboard, browser):
    page, errors = _open(browser, served_dashboard + "/")
    assert errors == [], errors
    text = page.evaluate("document.getElementById('pill-bot').textContent")
    assert "bots" in text or "bot" in text
    assert page.evaluate("!!document.getElementById('pill-reload')") is False
