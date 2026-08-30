"""First screen: which dashboard to connect to. Defaults to the local
instance's own .env (bot/envfile.py) when run on the same machine — a
remote/federated BotServer just needs its own host:port and token typed
in, same as the desktop app already supports.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Static

from bot.tui.client import ApiError, DashboardClient


class ConnectScreen(Screen):
    BINDINGS = [("escape", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        from bot import envfile

        default_token = envfile.get_var("DASHBOARD_TOKEN") or ""
        yield Static("BotServer — connect to a dashboard", id="title")
        with Vertical(id="connect-form"):
            yield Label("Host:port", classes="field-label")
            yield Input(value="127.0.0.1:8787", id="connect-host")
            yield Label("Dashboard token", classes="field-label")
            yield Input(value=default_token, password=True, id="connect-token")
            yield Button("Connect", id="connect-submit", variant="primary")
            yield Label("", id="connect-status", classes="status-line")
        yield Footer()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "connect-submit":
            return
        await self._connect()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._connect()

    async def _connect(self) -> None:
        status = self.query_one("#connect-status", Label)
        host = self.query_one("#connect-host", Input).value.strip()
        token = self.query_one("#connect-token", Input).value.strip()
        if not host or not token:
            status.update("Host and token are both required.")
            return
        status.update("Connecting…")
        base_url = host if host.startswith("http") else f"http://{host}"
        client = DashboardClient(base_url, token)
        try:
            await client.list_bots()
        except ApiError as exc:
            status.update(f"Failed: {exc}")
            await client.aclose()
            return
        except Exception as exc:  # network error, bad host, etc.
            status.update(f"Couldn't reach {base_url}: {exc}")
            await client.aclose()
            return
        self.app.client = client
        from bot.tui.screens.bot_list import BotListScreen

        self.app.push_screen(BotListScreen())
