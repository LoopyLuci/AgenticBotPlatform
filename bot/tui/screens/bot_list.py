"""Main screen — every configured bot, live status, and the lifecycle
actions the dashboard's Bots tab offers (start/stop/restart/enable/
disable/delete), plus entry points into add/edit."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Label

from bot.tui.client import ApiError

COLUMNS = ("id", "name", "platform", "backend", "status", "enabled")


class BotListScreen(Screen):
    BINDINGS = [
        ("a", "add_bot", "Add bot"),
        ("e", "edit_bot", "Edit"),
        ("c", "chat_bot", "Chat"),
        ("r", "refresh", "Refresh"),
        ("p", "providers", "Providers"),
        ("g", "agent_settings", "Agent settings"),
        ("w", "swarms", "Swarms"),
        ("s", "sessions", "Sessions"),
        ("h", "ssh_toolkit", "SSH Toolkit"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="bot-list-actions"):
            yield Button("Add bot (a)", id="btn-add")
            yield Button("Edit (e)", id="btn-edit")
            yield Button("Chat (c)", id="btn-chat")
            yield Button("Start/Stop", id="btn-startstop")
            yield Button("Enable/Disable", id="btn-toggle")
            yield Button("Restart", id="btn-restart")
            yield Button("Delete", id="btn-delete")
            yield Button("Refresh (r)", id="btn-refresh")
            yield Button("Providers (p)", id="btn-providers")
            yield Button("Agent settings (g)", id="btn-agent-settings")
            yield Button("Swarms (w)", id="btn-swarms")
            yield Button("Sessions (s)", id="btn-sessions")
            yield Button("SSH Toolkit (h)", id="btn-ssh-toolkit")
        yield DataTable(id="bot-table")
        yield Label("", id="bot-list-status")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#bot-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(*COLUMNS)
        await self.refresh_bots()

    async def refresh_bots(self) -> None:
        table = self.query_one("#bot-table", DataTable)
        status = self.query_one("#bot-list-status", Label)
        selected_key = table.cursor_row
        try:
            bots = await self.app.client.list_bots()
        except ApiError as exc:
            status.update(f"Failed to load bots: {exc}")
            return
        table.clear()
        self._bots = bots
        if not bots:
            status.update('No bots yet — press "a" to add your first one.')
            return
        status.update("")
        for b in bots:
            if b.get("platform") == "app":
                state = "app-only"
            else:
                state = "running" if b.get("live_running") else ("crashed" if b.get("last_error") else "stopped")
            table.add_row(
                str(b["id"]), b["name"], b["platform"], b["backend"], state,
                "yes" if b["enabled"] else "no", key=str(b["id"]),
            )
        if selected_key is not None and selected_key < table.row_count:
            table.move_cursor(row=selected_key)

    def _selected_bot(self) -> dict | None:
        table = self.query_one("#bot-table", DataTable)
        if table.cursor_row is None or not getattr(self, "_bots", None):
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((b for b in self._bots if str(b["id"]) == row_key), None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-add": self.action_add_bot,
            "btn-edit": self.action_edit_bot,
            "btn-chat": self.action_chat_bot,
            "btn-refresh": self.action_refresh,
            "btn-startstop": self._action_startstop,
            "btn-toggle": self._action_toggle,
            "btn-restart": self._action_restart,
            "btn-delete": self._action_delete,
            "btn-providers": self.action_providers,
            "btn-agent-settings": self.action_agent_settings,
            "btn-swarms": self.action_swarms,
            "btn-sessions": self.action_sessions,
            "btn-ssh-toolkit": self.action_ssh_toolkit,
        }
        handler = actions.get(event.button.id)
        if handler:
            await handler()

    async def action_add_bot(self) -> None:
        from bot.tui.screens.add_bot import AddBotScreen

        await self.app.push_screen(AddBotScreen(), self._after_form)

    async def action_edit_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        from bot.tui.screens.bot_detail import BotDetailScreen

        await self.app.push_screen(BotDetailScreen(bot), self._after_form)

    async def action_chat_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        from bot.tui.screens.chat import ChatScreen

        await self.app.push_screen(ChatScreen(bot))

    async def action_providers(self) -> None:
        from bot.tui.screens.providers import ProvidersScreen

        await self.app.push_screen(ProvidersScreen())

    async def action_agent_settings(self) -> None:
        from bot.tui.screens.agent_settings import AgentSettingsScreen

        bot = self._selected_bot()
        await self.app.push_screen(AgentSettingsScreen(instance_id=bot["id"] if bot else None))

    async def action_swarms(self) -> None:
        from bot.tui.screens.swarms import SwarmsScreen

        await self.app.push_screen(SwarmsScreen())

    async def action_sessions(self) -> None:
        from bot.tui.screens.sessions import SessionsScreen

        bot = self._selected_bot()
        await self.app.push_screen(SessionsScreen(instance_id=bot["id"] if bot else None))

    async def action_ssh_toolkit(self) -> None:
        from bot.tui.screens.ssh_toolkit import SshToolkitScreen

        await self.app.push_screen(SshToolkitScreen())

    async def _after_form(self, _result: object = None) -> None:
        await self.refresh_bots()

    async def action_refresh(self) -> None:
        await self.refresh_bots()

    async def _action_startstop(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        if bot.get("live_running"):
            await self.app.client.stop_bot(bot["id"])
        else:
            await self.app.client.start_bot(bot["id"])
        await self.refresh_bots()

    async def _action_toggle(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        if bot["enabled"]:
            await self.app.client.disable_bot(bot["id"])
        else:
            await self.app.client.enable_bot(bot["id"])
        await self.refresh_bots()

    async def _action_restart(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        await self.app.client.restart_bot(bot["id"])
        await self.refresh_bots()

    async def _action_delete(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        await self.app.client.delete_bot(bot["id"])
        await self.refresh_bots()
