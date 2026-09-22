"""Swarms (multi-bot fan-out/leader-vote/etc.) — list, create, enable/disable, run, and
watch recent runs. The TUI's counterpart to the dashboard's Swarms page."""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, TextArea

from bot.tui.client import ApiError

STRATEGIES = ("fanout_synthesize", "leader_vote", "sequential_relay", "decompose_delegate", "custom")
COLUMNS = ("id", "name", "strategy", "enabled")
RUN_COLUMNS = ("id", "swarm_run_id", "status")


class SwarmsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("Swarms", id="swarms-title")
        with Vertical(id="swarms-form"):
            with Horizontal():
                yield Input(placeholder="name", id="swarm-name")
                yield Select([(s, s) for s in STRATEGIES], id="swarm-strategy")
            yield TextArea('{"members": []}', id="swarm-config")
            yield Button("Create", id="swarm-create", variant="primary")
        yield DataTable(id="swarms-table")
        with Horizontal():
            yield Button("Enable/Disable selected", id="swarm-toggle")
            yield Button("Run selected", id="swarm-run")
            yield Button("Delete selected", id="swarm-delete")
            yield Button("Refresh", id="swarm-refresh")
        yield Input(placeholder="Prompt for Run…", id="swarm-prompt")
        yield Label("", id="swarms-status")
        yield Static("Recent runs", id="runs-title")
        yield DataTable(id="runs-table")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#swarms-table", DataTable).cursor_type = "row"
        self.query_one("#swarms-table", DataTable).add_columns(*COLUMNS)
        self.query_one("#runs-table", DataTable).cursor_type = "row"
        self.query_one("#runs-table", DataTable).add_columns(*RUN_COLUMNS)
        await self.refresh_all()

    async def refresh_all(self) -> None:
        status = self.query_one("#swarms-status", Label)
        try:
            self._swarms = await self.app.client.list_swarms()
            self._runs = await self.app.client.list_swarm_runs(limit=20)
        except ApiError as exc:
            status.update(f"Failed to load: {exc}")
            return
        status.update("")
        table = self.query_one("#swarms-table", DataTable)
        table.clear()
        for s in self._swarms:
            table.add_row(str(s["id"]), s["name"], s["strategy"], "yes" if s["enabled"] else "no", key=str(s["id"]))
        rtable = self.query_one("#runs-table", DataTable)
        rtable.clear()
        for r in self._runs:
            rtable.add_row(str(r.get("id", "")), str(r.get("swarm_run_id", ""))[:12], r.get("status", ""))

    def _selected(self) -> dict | None:
        table = self.query_one("#swarms-table", DataTable)
        if table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((s for s in self._swarms if str(s["id"]) == key), None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        status = self.query_one("#swarms-status", Label)
        bid = event.button.id
        if bid == "swarm-create":
            name = self.query_one("#swarm-name", Input).value.strip()
            strategy = self.query_one("#swarm-strategy", Select).value
            config_text = self.query_one("#swarm-config", TextArea).text
            if not name or not strategy or strategy is Select.BLANK:
                status.update("Name and strategy are both required.")
                return
            try:
                config = json.loads(config_text or "{}")
            except json.JSONDecodeError as exc:
                status.update(f"Config must be valid JSON: {exc}")
                return
            try:
                await self.app.client.create_swarm(name, str(strategy), config)
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Created {name!r}.")
            await self.refresh_all()
        elif bid == "swarm-toggle":
            swarm = self._selected()
            if swarm is None:
                return
            method = self.app.client.disable_swarm if swarm["enabled"] else self.app.client.enable_swarm
            await method(swarm["id"])
            await self.refresh_all()
        elif bid == "swarm-run":
            swarm = self._selected()
            if swarm is None:
                return
            prompt = self.query_one("#swarm-prompt", Input).value.strip()
            if not prompt:
                status.update("Enter a prompt first.")
                return
            try:
                result = await self.app.client.run_swarm(swarm["id"], prompt)
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Started run {result.get('swarm_run_id', '')}.")
            await self.refresh_all()
        elif bid == "swarm-delete":
            swarm = self._selected()
            if swarm is None:
                return
            await self.app.client.delete_swarm(swarm["id"])
            status.update(f"Deleted {swarm['name']!r}.")
            await self.refresh_all()
        elif bid == "swarm-refresh":
            await self.refresh_all()
