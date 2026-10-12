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
            for _ in range(50):  # the table redraws after the add round-trip; give a loaded machine time
                if table.row_count == len(providers):
                    break
                await pilot.pause(0.1)
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


def test_swarms_screen_creates_lists_and_runs(dashboard_client, monkeypatch):
    from types import SimpleNamespace

    from bot.router import router

    async def fake_ask(text, **kw):
        return SimpleNamespace(text="ok")
    monkeypatch.setattr(router, "ask", fake_ask)

    instance_id = _create_instance(name="swarm-member", platform="app", backend="native_agent",
                                   credentials={}, allowed_user_ids=[])

    async def _run():
        from textual.widgets import Button, DataTable, Input, Select, TextArea

        from bot.tui.screens.swarms import SwarmsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(SwarmsScreen())
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SwarmsScreen)

            screen.query_one("#swarm-name", Input).value = "s1"
            screen.query_one("#swarm-strategy", Select).value = "leader_vote"
            screen.query_one("#swarm-config", TextArea).text = f'{{"members": [{instance_id}], "leader": {instance_id}}}'
            screen.query_one("#swarm-create", Button).press()
            await pilot.pause()

            swarms = await dashboard_client.list_swarms()
            assert any(s["name"] == "s1" for s in swarms)
            table = screen.query_one("#swarms-table", DataTable)
            assert table.row_count == 1

            table.move_cursor(row=0)
            screen.query_one("#swarm-prompt", Input).value = "go"
            screen.query_one("#swarm-run", Button).press()
            await pilot.pause()

    asyncio.run(_run())


def test_sessions_screen_lists_and_deletes(dashboard_client):
    instance_id = _create_instance(name="session-target", platform="app", backend="native_agent",
                                   credentials={}, allowed_user_ids=[])

    async def _run():
        from bot.tui.screens.sessions import SessionsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(SessionsScreen(instance_id=instance_id))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SessionsScreen)
            # No real sessions exist yet for a brand-new bot - just confirm the screen
            # loads and queries the real route without raising.
            assert screen._sessions is not None

    asyncio.run(_run())


def test_ssh_toolkit_screen_adds_tests_and_removes(dashboard_client, monkeypatch, tmp_path):
    from bot import ssh_toolkit

    # SSH Toolkit is a PowerShell module in a git submodule: skip without pwsh, or in a checkout (e.g. a
    # fresh worktree) where vendor/ssh_toolkit has not been initialised.
    available, reason = ssh_toolkit.is_available()
    if not available:
        pytest.skip(f"SSH Toolkit is not available here: {reason}")
    # Isolated from the real ~/.ssh/config - see bot/ssh_toolkit.py's own comment on
    # this env var, and tests/test_abp_cli.py's identical fixture.
    monkeypatch.setenv("ABP_SSH_TOOLKIT_HOME", str(tmp_path))

    async def _run():
        from textual.widgets import Button, DataTable, Input, Label

        from bot.tui.screens.ssh_toolkit import SshToolkitScreen

        async def _wait_until(predicate, *, timeout=15.0, interval=0.2):
            # Each screen action here round-trips through a REAL PowerShell subprocess
            # (not just an in-memory DB write like every other screen's own tests), so
            # a fixed pilot.pause() can't be sized reliably - poll instead.
            elapsed = 0.0
            while elapsed < timeout:
                if predicate():
                    return
                await pilot.pause(interval)
                elapsed += interval
            assert predicate()

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(SshToolkitScreen())
            screen = app.screen
            assert isinstance(screen, SshToolkitScreen)
            await _wait_until(lambda: screen.query_one("#ssh-availability", Label).content == "")

            screen.query_one("#ssh-name", Input).value = "tui-test-ssh"
            screen.query_one("#ssh-hostname", Input).value = "10.9.9.9"
            screen.query_one("#ssh-add", Button).press()

            table = screen.query_one("#ssh-table", DataTable)
            await _wait_until(lambda: table.row_count == 1)

            table.move_cursor(row=0)
            await pilot.pause()
            screen.query_one("#ssh-test", Button).press()
            status = screen.query_one("#ssh-status", Label)
            await _wait_until(lambda: "not reachable" in str(status.content))

            screen.query_one("#ssh-remove", Button).press()
            await _wait_until(lambda: screen.query_one("#ssh-table", DataTable).row_count == 0)

    asyncio.run(_run())


# ---- the TUI follows the unit runtime --------------------------------------------------------------
class _Runtime:
    """A fake runtime behind DashboardClient.ui_manifest / runtime_events (everything else is the real API)."""

    def __init__(self, live):
        self.live, self.generation = set(live), 1

    def set(self, live):
        self.live, self.generation = set(live), self.generation + 1

    async def ui_manifest(self):
        return {"generation": self.generation,
                "panels": [{"id": u, "unit": u, "kind": "builtin", "section": u} for u in sorted(self.live)]}

    async def runtime_events(self, since=0):
        return {"generation": self.generation,
                "events": [{"generation": self.generation}] if self.generation > since else []}


def _offered(screen):
    from textual.widgets import Button

    return {a for a in ("hosting", "storage", "localai") if screen.query_one(f"#btn-{a}", Button).display}


def test_feature_screens_are_offered_only_while_their_unit_is_live(dashboard_client, monkeypatch):
    runtime = _Runtime({"hosting", "storage", "localai"})
    monkeypatch.setattr(dashboard_client, "ui_manifest", runtime.ui_manifest)
    monkeypatch.setattr(dashboard_client, "runtime_events", runtime.runtime_events)

    async def _run():
        app = AgenticBotPlatformTUI()
        app.UNIT_POLL_SECONDS = 0.1
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(BotListScreen())
            await pilot.pause()
            screen = app.screen
            assert _offered(screen) == {"hosting", "storage", "localai"}
            runtime.set({"storage", "localai"})              # hosting unloads: an event, then the screen is gone
            for _ in range(30):
                await pilot.pause(0.1)
                if _offered(screen) == {"storage", "localai"}:
                    break
            assert _offered(screen) == {"storage", "localai"}
            assert screen.check_action("hosting", ()) is False and screen.check_action("storage", ()) is True
            assert screen.check_action("providers", ()) is True      # a screen without a unit is always offered
            runtime.set({"hosting", "storage", "localai"})   # loaded again: it appears
            for _ in range(30):
                await pilot.pause(0.1)
                if "hosting" in _offered(screen):
                    break
            assert _offered(screen) == {"hosting", "storage", "localai"}
            assert screen.check_action("hosting", ()) is True

    asyncio.run(_run())


def test_a_server_without_a_ui_manifest_shows_everything(dashboard_client, monkeypatch):
    from bot.tui.client import ApiError

    async def no_manifest():
        raise ApiError(404, "Not Found")

    monkeypatch.setattr(dashboard_client, "ui_manifest", no_manifest)

    async def _run():
        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            app.client = dashboard_client
            await app.push_screen(BotListScreen())
            await pilot.pause()
            assert _offered(app.screen) == {"hosting", "storage", "localai"}
            assert app.units.available is False and app._unit_timer is None     # and it stops asking

    asyncio.run(_run())


# ---- list-and-act screens for what abp_cli covers (bot/tui/screens/manage.py) ----------------------
async def _wait(pilot, predicate, timeout=5.0):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await pilot.pause(0.1)
        elapsed += 0.1
    assert predicate()


async def _open(app, pilot, dashboard_client, screen_cls):
    app.client = dashboard_client
    await app.push_screen(screen_cls())
    screen = app.screen
    assert isinstance(screen, screen_cls)
    await pilot.pause()
    return screen


def _rows(screen, prefix):
    from textual.widgets import DataTable

    return screen.query_one(f"#{prefix}-table", DataTable).row_count


def _status(screen, prefix):
    from textual.widgets import Label

    return str(screen.query_one(f"#{prefix}-status", Label).content)


def _set(screen, prefix, **values):
    from textual.widgets import Input

    for suffix, value in values.items():
        screen.query_one(f"#{prefix}-{suffix}", Input).value = value


def _press(screen, bid):
    from textual.widgets import Button

    screen.query_one(f"#{bid}", Button).press()


def _select_first(screen, prefix):
    from textual.widgets import DataTable

    screen.query_one(f"#{prefix}-table", DataTable).move_cursor(row=0)


async def _confirm(pilot, app, yes=True):
    from bot.tui.screens.manage import ConfirmScreen

    await _wait(pilot, lambda: isinstance(app.screen, ConfirmScreen))
    app.screen.query_one("#confirm-yes" if yes else "#confirm-no").press()
    await pilot.pause()


def test_key_bindings_for_the_new_screens_are_registered_on_the_bot_list():
    keys = {key: action for key, action, _ in BotListScreen.BINDINGS}
    assert {keys[k] for k in "kmuibyx"} == {"hooks", "mcp", "plugins", "skills", "snapshots", "security", "peers"}
    assert len(keys) == len(BotListScreen.BINDINGS)      # no key bound twice


def test_hooks_screen_adds_disables_and_removes_with_confirm(dashboard_client):
    async def _run():
        from bot.tui.screens.manage import HooksScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, HooksScreen)
            assert _rows(screen, "hooks") == 0
            _press(screen, "hooks-add")                       # nothing typed: a message, no request
            await pilot.pause()
            assert "required" in _status(screen, "hooks")
            _set(screen, "hooks", event="PreToolUse", command="echo hi", matcher="Bash")
            _press(screen, "hooks-add")
            await _wait(pilot, lambda: _rows(screen, "hooks") == 1)
            _select_first(screen, "hooks")
            _press(screen, "hooks-disable")
            await _wait(pilot, lambda: "Disabled" in _status(screen, "hooks"))
            assert screen._rows[0]["enabled"] is False
            _press(screen, "hooks-remove")                    # declined: the hook stays
            await _confirm(pilot, app, yes=False)
            assert _rows(screen, "hooks") == 1
            _press(screen, "hooks-remove")
            await _confirm(pilot, app, yes=True)
            await _wait(pilot, lambda: _rows(screen, "hooks") == 0)
            _set(screen, "hooks", event="NotAnEvent", command="x")   # the API's refusal is shown, not raised
            _press(screen, "hooks-add")
            await _wait(pilot, lambda: "Failed" in _status(screen, "hooks"))

    asyncio.run(_run())


def test_mcp_screen_adds_external_without_showing_the_token_and_removes_it(dashboard_client, monkeypatch):
    from bot.agent_runtime import mcp_client

    async def _no_connect(name):
        return False

    monkeypatch.setattr(mcp_client, "connect", _no_connect)

    async def _run():
        from textual.widgets import DataTable, Input

        from bot.tui.screens.manage import McpScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, McpScreen)
            _set(screen, "mcp", name="tui-ext", transport="stdio", command="python", args='["-m", "srv"]',
                 token="SECRET-TOKEN-VALUE")
            _press(screen, "mcp-add")
            await _wait(pilot, lambda: any(r["name"] == "tui-ext" for r in screen._rows))
            row = next(r for r in screen._rows if r["name"] == "tui-ext")
            assert row["kind"] == "external" and row["has_auth_token"] is True
            table = screen.query_one("#mcp-table", DataTable)
            shown = " ".join(str(c) for r in range(table.row_count) for c in table.get_row_at(r))
            assert "SECRET-TOKEN-VALUE" not in shown and "set" in shown
            assert screen.query_one("#mcp-token", Input).value == ""          # cleared once sent
            table.move_cursor(row=[r["name"] for r in screen._rows].index("tui-ext"))
            _press(screen, "mcp-disable")
            await _wait(pilot, lambda: not next(r for r in screen._rows if r["name"] == "tui-ext")["enabled"])
            _press(screen, "mcp-remove")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: not any(r["name"] == "tui-ext" for r in screen._rows))

    asyncio.run(_run())


def test_plugins_screen_installs_disables_and_removes_with_confirm(dashboard_client, tmp_path):
    plugin = tmp_path / "tui_screen_plugin.py"
    plugin.write_text("PLUGIN_DESCRIPTION = 'x'\n\ndef setup(api):\n    pass\n", encoding="utf-8")

    async def _run():
        from bot.tui.screens.manage import PluginsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, PluginsScreen)
            _set(screen, "plugins", path=str(plugin))
            _press(screen, "plugins-add")
            await _wait(pilot, lambda: any(r["name"] == "tui_screen_plugin" for r in screen._rows))
            _select_first(screen, "plugins")
            _press(screen, "plugins-disable")
            await _wait(pilot, lambda: "Disabled" in _status(screen, "plugins"))
            _press(screen, "plugins-remove")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: _rows(screen, "plugins") == 0)

    asyncio.run(_run())


def test_skills_screen_adds_and_removes_with_confirm(dashboard_client):
    iid = _create_instance(name="skill-tui", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])

    async def _run():
        from bot.tui.screens.manage import SkillsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, SkillsScreen)
            _set(screen, "skills", instance=str(iid), name="greet", description="says hi", content="Say hi.")
            _press(screen, "skills-add")
            await _wait(pilot, lambda: "Added" in _status(screen, "skills"))
            assert "greet" in [s["name"] for s in await dashboard_client.list_skills(instance_id=iid)]
            _set(screen, "skills", instance="", name="globalskill", description="d", content="body")
            _press(screen, "skills-add")
            await _wait(pilot, lambda: any(r["name"] == "globalskill" for r in screen._rows))
            _select_first(screen, "skills")
            _press(screen, "skills-remove")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: not any(r["name"] == "globalskill" for r in screen._rows))

    asyncio.run(_run())


def test_snapshots_screen_creates_restores_and_deletes_with_confirm(dashboard_client, monkeypatch, tmp_path):
    from bot import snapshots

    monkeypatch.setattr(snapshots, "SNAPSHOTS_ROOT", tmp_path / "snapshots")
    # restoring would overwrite the live config/db: stub the restore itself; the confirm and the API route are what is tested
    restored = []
    monkeypatch.setattr(snapshots, "restore_snapshot", lambda name, *a, **k: restored.append(name) or {"name": name})

    async def _run():
        from bot.tui.screens.manage import SnapshotsScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, SnapshotsScreen)
            _set(screen, "snapshots", label="tui")
            _press(screen, "snapshots-add")
            await _wait(pilot, lambda: _rows(screen, "snapshots") == 1)
            _select_first(screen, "snapshots")
            _press(screen, "snapshots-restore")
            await _confirm(pilot, app, yes=False)
            assert restored == []
            _press(screen, "snapshots-restore")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: len(restored) == 1)
            _press(screen, "snapshots-delete")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: _rows(screen, "snapshots") == 0)

    asyncio.run(_run())


def test_security_screen_adds_and_removes_an_allowed_user_with_confirm(dashboard_client):
    async def _run():
        from bot.tui.screens.manage import SecurityScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, SecurityScreen)
            _set(screen, "security", telegram_id="abc")
            _press(screen, "security-add")
            await pilot.pause()
            assert "numeric" in _status(screen, "security")
            _set(screen, "security", telegram_id="424242", name="Ann")
            _press(screen, "security-add")
            await _wait(pilot, lambda: any(r["telegram_id"] == 424242 for r in screen._rows))
            screen.query_one("#security-table").move_cursor(row=[r["telegram_id"] for r in screen._rows].index(424242))
            _press(screen, "security-remove")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: not any(r["telegram_id"] == 424242 for r in screen._rows))

    asyncio.run(_run())


def test_peers_screen_lists_and_unlinks_with_confirm(dashboard_client):
    from bot import db

    key_id, _ = db.create_api_key("peer: Server B", kind="peer_server")
    db.create_peer_server("Server B", "http://10.0.2.15:8787", "unused", key_id)

    async def _run():
        from bot.tui.screens.manage import PeersScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, PeersScreen)
            assert _rows(screen, "peers") == 1
            _press(screen, "peers-add")                       # no token typed
            await pilot.pause()
            assert "required" in _status(screen, "peers")
            _select_first(screen, "peers")
            _press(screen, "peers-remove")
            await _confirm(pilot, app, yes=False)
            assert _rows(screen, "peers") == 1
            _press(screen, "peers-remove")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: _rows(screen, "peers") == 0)

    asyncio.run(_run())


def test_key_bindings_for_devices_env_and_kanban_are_registered():
    keys = {key: action for key, action, _ in BotListScreen.BINDINGS}
    assert (keys["j"], keys["z"], keys["1"]) == ("devices", "env", "kanban")
    assert len(keys) == len(BotListScreen.BINDINGS)


def test_devices_screen_lists_online_state_and_revokes_with_confirm(dashboard_client):
    from bot import db

    key_id, plaintext = db.create_api_key("Pixel", kind="device")

    async def _run():
        from bot.tui.screens.manage import DevicesScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, DevicesScreen)
            assert _rows(screen, "devices") == 1
            assert screen._rows[0]["label"] == "Pixel" and screen._rows[0]["online"] is False
            assert "mobile key" in str(screen.query_one("#devices-title").content)      # pairing is CLI/GUI only
            assert not screen.query("#devices-add")                                      # no way to mint a key here
            assert plaintext not in app.export_screenshot()
            _select_first(screen, "devices")
            _press(screen, "devices-revoke")
            await _confirm(pilot, app, yes=False)
            assert _rows(screen, "devices") == 1
            _press(screen, "devices-revoke")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: _rows(screen, "devices") == 0)
            assert db.get_api_key(key_id)["revoked_at"]

    asyncio.run(_run())


def test_env_screen_masks_values_sets_one_and_never_shows_a_secret(dashboard_client, monkeypatch, tmp_path):
    from bot import envfile

    env = tmp_path / "test.env"
    env.write_text("# comment\nDASHBOARD_TOKEN=test-token\nSECRET_KEY=hunter2-SECRET-VALUE\nEMPTY_ONE=\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "PROJECT_ENV", env)
    monkeypatch.setattr(envfile, "GLOBAL_ENV", tmp_path / "none.env")
    monkeypatch.setattr(envfile, "CONFIG_PATH", tmp_path / "no-config.yaml")
    monkeypatch.setattr(envfile, "BACKUP_DIR", tmp_path / "env_backups")

    async def _run():
        from textual.widgets import Input

        from bot.tui.screens.manage import EnvScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, EnvScreen)
            assert [r["key"] for r in screen._rows] == ["DASHBOARD_TOKEN", "SECRET_KEY", "EMPTY_ONE"]
            assert screen._rows[1]["set"] is True and screen._rows[2]["set"] is False
            assert "hunter2" not in app.export_screenshot() and "********" in app.export_screenshot()
            _set(screen, "env", key="NEW_THING", value="brand-new-SECRET-2")
            assert screen.query_one("#env-value", Input).password is True
            _press(screen, "env-add")
            await _wait(pilot, lambda: any(r["key"] == "NEW_THING" for r in screen._rows))
            assert screen.query_one("#env-value", Input).value == ""                     # cleared after sending
            assert "NEW_THING=brand-new-SECRET-2" in env.read_text(encoding="utf-8")
            _press(screen, "env-add")                                                    # nothing typed
            await pilot.pause()
            assert "required" in _status(screen, "env")
            _set(screen, "env", key="bad=key", value="rejected-SECRET-3")                # the API refuses the key
            _press(screen, "env-add")
            await _wait(pilot, lambda: "Failed" in _status(screen, "env"))
            assert "rejected-SECRET-3" not in _status(screen, "env")
            assert screen.query_one("#env-value", Input).value == ""
            _press(screen, "env-reload")
            await _wait(pilot, lambda: "reloaded" in _status(screen, "env"))
            _press(screen, "env-diag")
            await _wait(pilot, lambda: "system_info" in screen.query_one("#env-detail").text)
            shown = app.export_screenshot() + screen.query_one("#env-detail").text
            for secret in ("hunter2-SECRET-VALUE", "brand-new-SECRET-2", "rejected-SECRET-3"):
                assert secret not in shown

    asyncio.run(_run())


def test_kanban_screen_adds_moves_and_deletes_a_card_with_confirm(dashboard_client):
    iid = _create_instance(name="kanban-tui")

    async def _run():
        from bot.tui.screens.manage import KanbanScreen

        app = AgenticBotPlatformTUI()
        async with app.run_test(size=(140, 100)) as pilot:
            screen = await _open(app, pilot, dashboard_client, KanbanScreen)
            assert _rows(screen, "kanban") == 0
            _press(screen, "kanban-add")                                                 # no text typed
            await pilot.pause()
            assert "required" in _status(screen, "kanban")
            _set(screen, "kanban", instance=str(iid), board="work", text="write docs")
            _press(screen, "kanban-add")
            await _wait(pilot, lambda: _rows(screen, "kanban") == 1)
            assert screen._rows[0]["column_name"] == "todo"
            _press(screen, "kanban-boards")
            await _wait(pilot, lambda: "work" in screen.query_one("#kanban-detail").text)
            _select_first(screen, "kanban")
            _set(screen, "kanban", column="doing")
            _press(screen, "kanban-move")
            await _wait(pilot, lambda: screen._rows and screen._rows[0]["column_name"] == "doing")
            _press(screen, "kanban-delete")
            await _confirm(pilot, app, yes=False)
            assert _rows(screen, "kanban") == 1
            _press(screen, "kanban-delete")
            await _confirm(pilot, app)
            await _wait(pilot, lambda: _rows(screen, "kanban") == 0)

    asyncio.run(_run())
