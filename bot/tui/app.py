"""AgenticBotPlatform TUI — the terminal-world equivalent of the browser dashboard,
built on Textual. Talks to a running AgenticBotPlatform's dashboard HTTP API over
bot/tui/client.py's DashboardClient, never importing bot.* business-logic
modules directly, so it works against a remote/federated install exactly
like the desktop app already does. Run with `python -m bot.tui`.

Bootstrapping the .env this app needs to even start (ANTHROPIC_API_KEY,
DASHBOARD_TOKEN) is scripts/setup.py's job, not this one's — this is the
ongoing "manage bots" terminal app, used after a AgenticBotPlatform instance is
already up, the same way the dashboard is used after the GUI setup
wizard finishes.
"""

from __future__ import annotations

from textual.app import App

from bot.tui.client import DashboardClient
from bot.tui.screens.connect import ConnectScreen
from bot.tui.units import UnitFollower


class AgenticBotPlatformTUI(App):
    """Root app — owns the one DashboardClient for the whole session."""

    TITLE = "AgenticBotPlatform"
    CSS_PATH = "app.tcss"

    client: DashboardClient | None = None
    UNIT_POLL_SECONDS = 3.0

    def __init__(self) -> None:
        super().__init__()
        self.units = UnitFollower()
        self._unit_timer = None

    def on_mount(self) -> None:
        self.push_screen(ConnectScreen())

    def unit_visible(self, unit: str | None) -> bool:
        """Whether a screen of `unit` is offered: only while the unit is live (everything, if the server cannot say)."""
        return self.units.visible(unit)

    async def follow_units(self) -> None:
        """Read the manifest once and keep following the runtime's events. Safe to call again."""
        if self.client is None:
            return
        await self._units_changed(await self.units.refresh(self.client))
        if self._unit_timer is None and self.units.available:
            self._unit_timer = self.set_interval(self.UNIT_POLL_SECONDS, self._poll_units)

    async def _poll_units(self) -> None:
        if self.client is None:
            return
        await self._units_changed(await self.units.poll(self.client))
        if self.units.available is False and self._unit_timer is not None:
            self._unit_timer.stop()
            self._unit_timer = None

    async def _units_changed(self, changed: bool) -> None:
        if not changed:
            return
        self.refresh_bindings()
        for screen in self.screen_stack:
            apply = getattr(screen, "apply_units", None)
            if apply is not None:
                apply()

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def main() -> None:
    AgenticBotPlatformTUI().run()


if __name__ == "__main__":
    main()
