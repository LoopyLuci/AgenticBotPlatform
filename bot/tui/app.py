"""BotServer TUI — the terminal-world equivalent of the browser dashboard,
built on Textual. Talks to a running BotServer's dashboard HTTP API over
bot/tui/client.py's DashboardClient, never importing bot.* business-logic
modules directly, so it works against a remote/federated install exactly
like the desktop app already does. Run with `python -m bot.tui`.

Bootstrapping the .env this app needs to even start (ANTHROPIC_API_KEY,
DASHBOARD_TOKEN) is scripts/setup.py's job, not this one's — this is the
ongoing "manage bots" terminal app, used after a BotServer instance is
already up, the same way the dashboard is used after the GUI setup
wizard finishes.
"""

from __future__ import annotations

from textual.app import App

from bot.tui.client import DashboardClient
from bot.tui.screens.connect import ConnectScreen


class BotServerTUI(App):
    """Root app — owns the one DashboardClient for the whole session."""

    TITLE = "BotServer"
    CSS_PATH = "app.tcss"

    client: DashboardClient | None = None

    def on_mount(self) -> None:
        self.push_screen(ConnectScreen())

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def main() -> None:
    BotServerTUI().run()


if __name__ == "__main__":
    main()
