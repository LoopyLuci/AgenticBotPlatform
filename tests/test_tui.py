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

import httpx
import pytest

from bot import bot_instances
from bot.dashboard.server import build_app
from bot.tui.app import BotServerTUI
from bot.tui.client import DashboardClient
from bot.tui.screens.add_bot import AddBotScreen
from bot.tui.screens.bot_detail import BotDetailScreen
from bot.tui.screens.bot_list import BotListScreen


@pytest.fixture
def dashboard_client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
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
        app = BotServerTUI()
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

        app = BotServerTUI()
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

        app = BotServerTUI()
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
        app = BotServerTUI()
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
