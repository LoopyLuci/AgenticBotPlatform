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
def served_dashboard(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    # The pages fetch each provider's models; never let a test read the real providers.yaml or reach out over the network.
    from bot import providers
    from bot.config import ConfigManager

    empty = tmp_path / "providers.yaml"
    empty.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=empty))
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


# ------------------------------------------------------------------ the ABP Agents page
@pytest.fixture
def agent_bot(temp_db):
    from bot import db

    conn = db.get_conn()
    conn.execute("INSERT INTO bot_instances (name, platform, backend, model, credentials, enabled, created_at, updated_at) "
                 "VALUES ('Research bot', 'telegram', 'native_agent', 'local/model', '{}', 1, datetime('now'), datetime('now'))")
    conn.commit()


def _agents_page(browser, base, tmp_path, monkeypatch):
    """Open the Agents page with config/backends.yaml pointed at a temp copy, so saving never touches the real one."""
    import shutil

    from bot.config import config

    copy = tmp_path / "backends.yaml"
    shutil.copy(config.path, copy)
    monkeypatch.setattr(config, "path", copy)
    page, errors = _open(browser, base + "/")
    page.wait_for_function("window.abpAgents && window.abpAgents.state.loaded", timeout=15000)
    return page, errors, copy


def test_the_agents_page_lists_tabs_bots_and_readiness(served_dashboard, browser, agent_bot, tmp_path, monkeypatch):
    page, errors, _ = _agents_page(browser, served_dashboard, tmp_path, monkeypatch)
    assert errors == [], errors
    tabs = page.evaluate("[...document.querySelectorAll('.agents-tab')].map(t => t.textContent.trim())")
    assert tabs == ["Overview", "Runtime", "Safety", "Tools", "Sub-agents & swarms", "Skills", "Models"]
    page.wait_for_function("document.querySelector('#agents-body .ag-check')", timeout=15000)
    assert "Research bot" in page.evaluate("document.getElementById('agents-body').textContent")
    assert page.evaluate("document.querySelectorAll('#agents-body .ag-check').length") >= 5


def test_editing_a_setting_saves_it_and_a_bad_value_is_refused_with_a_reason(served_dashboard, browser, agent_bot, tmp_path, monkeypatch):
    import yaml

    page, errors, copy = _agents_page(browser, served_dashboard, tmp_path, monkeypatch)
    page.evaluate("window.abpAgents.setTab('runtime')")
    box = page.locator('[data-f="native_agent.limits.max_iterations"]')
    box.fill("9")
    assert page.locator("#ag-save").is_visible()
    page.locator("#ag-save").click()
    page.wait_for_function("document.getElementById('agents-savebar').classList.contains('hidden')", timeout=10000)
    assert yaml.safe_load(copy.read_text(encoding="utf-8"))["native_agent"]["limits"]["max_iterations"] == 9

    bad = page.locator('[data-f="native_agent.context.compact_at"]')
    bad.fill("5")
    page.locator("#ag-save").click()
    err = page.locator('[data-row="native_agent.context.compact_at"] .ag-err')
    err.wait_for(state="visible", timeout=10000)
    assert "at most" in err.inner_text()
    assert yaml.safe_load(copy.read_text(encoding="utf-8"))["native_agent"]["context"]["compact_at"] != 5
    assert errors == [], errors


def test_a_dangerous_setting_shows_its_warning_only_while_it_applies(served_dashboard, browser, agent_bot, tmp_path, monkeypatch):
    page, errors, _ = _agents_page(browser, served_dashboard, tmp_path, monkeypatch)
    page.evaluate("window.abpAgents.setTab('safety')")
    warning = page.locator('[data-row="native_agent.permissions.allow_bypass"] .ag-danger')
    assert not warning.is_visible()
    page.locator('[data-f="native_agent.permissions.allow_bypass"]').check()
    assert warning.is_visible()
    page.locator('[data-f="native_agent.permissions.allow_bypass"]').uncheck()
    assert not warning.is_visible()


def test_the_agent_bot_button_opens_that_bots_settings(served_dashboard, browser, agent_bot, tmp_path, monkeypatch):
    page, errors, _ = _agents_page(browser, served_dashboard, tmp_path, monkeypatch)
    page.wait_for_function("document.querySelector('[data-bot-agent]')", timeout=20000)
    page.locator("[data-bot-agent]").first.click()
    assert page.evaluate("window.abpAgents.state.tab") == "subagents"
    assert page.evaluate("document.getElementById('agent-settings-instance').value") != ""
    assert errors == [], errors


# ------------------------------------------------------------------ the ABP Agent part of the bot form
def _bot_id():
    from bot import db

    return db.get_conn().execute("select id from bot_instances where name='Research bot'").fetchone()["id"]


def _api(page, method, path, body=None):
    return page.evaluate(
        "async ([m, p, b]) => { const r = await fetch(p, {method: m, headers: {'X-Dashboard-Token': getToken(), 'Content-Type': 'application/json'}, body: b ? JSON.stringify(b) : undefined}); return r.json(); }",
        [method, path, body])


def test_the_form_defaults_to_abp_agent_and_shows_its_settings(served_dashboard, browser):
    for path in ("/", "/desktop-ui/"):
        page, errors = _open(browser, served_dashboard + path)
        assert errors == [], errors
        assert page.evaluate("document.getElementById('bot-new-backend').value") == "native_agent"
        panel = page.locator("#bot-agent-panel")
        assert panel.is_visible()
        options = page.evaluate("[...document.getElementById('bot-agent-permission').options].map(o => o.value)")
        assert options == ["", "plan", "default", "accept_edits", "bypass"]
        page.wait_for_function("document.getElementById('bot-agent-global').textContent.includes('Global defaults')", timeout=10000)
        page.select_option("#bot-new-backend", "cli")
        assert not panel.is_visible()
        page.select_option("#bot-new-backend", "custom_model")
        assert panel.is_visible()  # every backend that runs the ABP agent loop gets the panel


def test_editing_a_bot_shows_only_what_it_set_itself_and_saves_only_what_changed(served_dashboard, browser, agent_bot):
    page, errors = _open(browser, served_dashboard + "/")
    bot = _bot_id()
    _api(page, "POST", "/api/agent-settings", {"instance_id": None, "max_concurrent_children": 9})  # a process-wide default
    _api(page, "PUT", f"/api/instances/{bot}/permissions", {"mode": "plan"})
    _api(page, "POST", "/api/agent-settings", {"instance_id": bot, "worker_provider": "local", "worker_model": "small", "worker_effort": "low"})

    page.evaluate(f"abpBotAgentForm.load({bot})")
    page.wait_for_function("document.getElementById('bot-agent-permission').value === 'plan'", timeout=10000)
    assert page.input_value("#bot-agent-worker-model") == "local/small"
    assert page.input_value("#bot-agent-worker-effort") == "low"
    assert page.input_value("#bot-agent-max-children") == ""                      # the 9 is inherited, not the bot's own
    assert page.get_attribute("#bot-agent-max-children", "placeholder") == "9 (default)"

    # saving with nothing changed sends nothing
    seen = []
    page.on("request", lambda r: seen.append((r.method, r.url)) if r.method in ("PUT", "POST") else None)
    page.evaluate(f"abpBotAgentForm.save({bot})")
    page.wait_for_timeout(800)
    assert seen == []

    # change a few things, and only those are written
    page.fill("#bot-agent-max-children", "4")
    page.select_option("#bot-agent-permission", "accept_edits")
    page.check("#bot-agent-plan-approval")
    page.evaluate(f"abpBotAgentForm.save({bot})")
    page.wait_for_function(f"fetch('/api/agent-settings?instance_id={bot}&own=true', {{headers: {{'X-Dashboard-Token': getToken()}}}}).then(r => r.json()).then(o => o.max_concurrent_children === 4)", timeout=10000)
    own = _api(page, "GET", f"/api/agent-settings?instance_id={bot}&own=true")
    assert own["max_concurrent_children"] == 4 and own["require_plan_approval"] is True
    assert own["worker_model"] == "small" and own["worker_effort"] == "low"      # untouched values kept
    assert _api(page, "GET", f"/api/instances/{bot}/permissions")["instance"]["mode"] == "accept_edits"
    assert errors == [], errors


def test_a_new_bot_left_at_the_defaults_writes_no_agent_settings_and_clearing_follows_the_default(served_dashboard, browser, agent_bot):
    page, errors = _open(browser, served_dashboard + "/")
    bot = _bot_id()
    page.evaluate("abpBotAgentForm.reset()")
    seen = []
    page.on("request", lambda r: seen.append(r.url) if r.method in ("PUT", "POST") else None)
    page.evaluate(f"abpBotAgentForm.save({bot})")
    page.wait_for_timeout(600)
    assert seen == []

    _api(page, "PUT", f"/api/instances/{bot}/permissions", {"mode": "plan"})
    page.evaluate(f"abpBotAgentForm.load({bot})")
    page.wait_for_function("document.getElementById('bot-agent-permission').value === 'plan'", timeout=10000)
    page.select_option("#bot-agent-permission", "")
    page.evaluate(f"abpBotAgentForm.save({bot})")
    page.wait_for_function(f"fetch('/api/instances/{bot}/permissions', {{headers: {{'X-Dashboard-Token': getToken()}}}}).then(r => r.json()).then(o => !o.instance.mode)", timeout=10000)
    assert errors == [], errors
