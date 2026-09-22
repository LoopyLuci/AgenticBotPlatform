"""bot/tui/ — exercised with Textual's own App.run_test() harness, with
DashboardClient's httpx.AsyncClient pointed at an in-process ASGITransport
wrapping the real dashboard build_app(), so these tests hit real request/
response handling (the same route wiring the web dashboard uses) rather
than a mock. Matches this codebase's convention (see tests/test_retention.py)
of plain sync test functions driving their async body via asyncio.run(),
not pytest-asyncio markers.
"""

from __future__ import annotations

import asyncio
import shutil

import httpx
import pytest

from bot import bot_instances
from bot.config import config
from bot.dashboard.server import build_app
from bot.tui.app import AgenticBotPlatformTUI
from bot.tui.client import DashboardClient
from bot.tui.screens.add_bot import AddBotScreen
from bot.tui.screens.bot_detail import BotDetailScreen
from bot.tui.screens.bot_list import BotListScreen


@pytest.fixture
def dashboard_client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    # config (config/backends.yaml) is a module-level singleton read by the real
    # /api/agent/config* routes the agent-settings screen hits — without redirecting it
    # to a throwaway copy, a test that saves a setting writes the REAL project config
    # file (same isolation tests/test_agent_settings_schema.py's own temp_config fixture
    # gives POST /api/agent/config directly).
    shipped = config.path
    temp_path = tmp_path / "backends.yaml"
    shutil.copy(shipped, temp_path)
    monkeypatch.setattr(config, "path", temp_path)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    return DashboardClient("http://testserver", "test-token", transport=transport)


def _create_instance(**overrides):
    creds = overrides.pop("credentials", None) or {"bot_token": "123456789:AAExampleTokenFromBotFather1234"}
    return bot_instances.create_instance(
        name=overrides.pop("name", "tui-bot"), platform=overrides.pop("platform", "telegram"),
        backend=overrides.pop("backend", "cli"), credentials=creds,
        allowed_user_ids=overrides.pop("allowed_user_ids", [111]), enabled=overrides.pop("enabled", False),
        **overrides,
    )


def test_bot_list_screen_renders_real_bots(dashboard_client):
    _create_instance(name="alpha")
    _create_instance(name="beta")

    async def _run():
        app = AgenticBotPlatformTUI()
        async with app.run_test() as pilot:
            app.client = dashboard_client
            await app.push_screen(BotListScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BotListScreen)
            names = {b["name"] for b in screen._bots}
            assert names == {"alpha", "beta"}

    asyncio.run(_run())


def test_bot_list_screen_empty_state(dashboard_client):
    async def _run():
        from textual.widgets import Label

        app = AgenticBotPlatformTUI()
        async with app.run_test() as pilot:
            app.client = dashboard_client
            await app.push_screen(BotListScreen())
            await pilot.pause()
            status = app.screen.query_one("#bot-list-status", Label)
            assert "no bots yet" in str(status.content).lower()

    asyncio.run(_run())


def test_add_bot_flow_creates_a_real_row(dashboard_client):
    async def _run():
        from textual.widgets import Input, Select

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(120, 60)) as pilot:
            app.client = dashboard_client
            await app.push_screen(AddBotScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AddBotScreen)
            screen.query_one("#field-name", Input).value = "new-tui-bot"
            screen.query_one("#field-platform", Select).value = "telegram"
            await pilot.pause()
            screen.query_one("#cred-bot_token", Input).value = "123456789:AAExampleTokenFromBotFather1234"
            screen.query_one("#field-allowed", Input).value = "111"
            await pilot.click("#submit")
            await pilot.pause()

            bots = await dashboard_client.list_bots()
            assert any(b["name"] == "new-tui-bot" for b in bots)

    asyncio.run(_run())


def test_bot_detail_screen_edits_and_schedules(dashboard_client):
    instance_id = _create_instance(name="editable-bot")

    async def _run():
        from textual.widgets import Button, Input

        bot = await dashboard_client.get_bot(instance_id)
        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(120, 80)) as pilot:
            app.client = dashboard_client
            await app.push_screen(BotDetailScreen(bot))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BotDetailScreen)

            screen.query_one("#sched-chatid", Input).value = "42"
            screen.query_one("#sched-interval", Input).value = "10m"
            screen.query_one("#sched-prompt", Input).value = "ping"
            screen.query_one("#sched-add", Button).press()
            await pilot.pause()

            schedules = await dashboard_client.list_schedules(instance_id)
            assert len(schedules) == 1
            assert schedules[0]["prompt"] == "ping"

    asyncio.run(_run())


def test_chat_screen_sends_a_real_message_and_shows_the_reply(dashboard_client, monkeypatch):
    from types import SimpleNamespace

    from bot.router import router
    from bot.tui.screens.chat import ChatScreen

    async def fake_ask(text, **kw):
        return SimpleNamespace(text=f"echo: {text}")
    monkeypatch.setattr(router, "ask", fake_ask)

    instance_id = _create_instance(name="chat-bot", platform="app", backend="native_agent",
                                   credentials={}, allowed_user_ids=[])

    async def _run():
        from textual.widgets import Input, RichLog

        bot = await dashboard_client.get_bot(instance_id)
        app = AgenticBotPlatformTUI()
        async with app.run_test() as pilot:
            app.client = dashboard_client
            await app.push_screen(ChatScreen(bot))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ChatScreen)
            screen.query_one("#chat-input", Input).value = "hello"
            await pilot.press("enter")
            await pilot.pause()
            log = screen.query_one("#chat-log", RichLog)
            text = "\n".join(str(line) for line in log.lines)
            assert "hello" in text and "echo: hello" in text

    asyncio.run(_run())


def test_providers_screen_adds_lists_and_removes(dashboard_client):
    async def _run():
        from textual.widgets import Button, DataTable, Input

        from bot.tui.screens.providers import ProvidersScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(120, 80)) as pilot:
            app.client = dashboard_client
            await app.push_screen(ProvidersScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ProvidersScreen)

            screen.query_one("#prov-name", Input).value = "tui-test-provider"
            screen.query_one("#prov-baseurl", Input).value = "http://127.0.0.1:11434/v1"
            screen.query_one("#prov-add", Button).press()
            await pilot.pause()

            providers = await dashboard_client.list_providers()
            assert any(p["name"] == "tui-test-provider" for p in providers)

            table = screen.query_one("#providers-table", DataTable)
            assert table.row_count == len(providers)

    asyncio.run(_run())


def test_agent_settings_screen_loads_global_schema_and_saves(dashboard_client):
    async def _run():
        from textual.widgets import Button, Checkbox

        from bot.tui.screens.agent_settings import AgentSettingsScreen, _widget_id

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(AgentSettingsScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AgentSettingsScreen)
            assert screen._schema.get("fields")

            box = screen.query_one(f"#{_widget_id('native_agent.web.enabled')}", Checkbox)
            box.value = True
            screen.query_one("#as-save", Button).press()
            await pilot.pause()

            config = await dashboard_client.get_agent_config()
            assert config["values"]["native_agent.web.enabled"] is True

    asyncio.run(_run())


def test_agent_settings_screen_for_one_bot_shows_its_own_section(dashboard_client):
    instance_id = _create_instance(name="settings-target", platform="app", backend="native_agent",
                                   credentials={}, allowed_user_ids=[])

    async def _run():
        from textual.widgets import Input

        from bot.tui.screens.agent_settings import AgentSettingsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(AgentSettingsScreen(instance_id=instance_id))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AgentSettingsScreen)

            screen.query_one("#f-own-worker_effort", Input).value = "high"
            from textual.widgets import Button
            screen.query_one("#as-save", Button).press()
            await pilot.pause()

            own = await dashboard_client.get_agent_settings(instance_id, own=True)
            assert own["worker_effort"] == "high"

    asyncio.run(_run())
