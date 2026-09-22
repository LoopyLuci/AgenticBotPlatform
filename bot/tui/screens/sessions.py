"""Browse and delete a bot's conversation sessions - the TUI's counterpart to the
dashboard's Sessions view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static, TextArea

from bot.tui.client import ApiError

COLUMNS = ("id", "instance_id", "title", "item_count")


class SessionsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, instance_id: int | None = None):
        super().__init__()
        self._instance_id = instance_id

    def compose(self) -> ComposeResult:
        yield Static("Sessions", id="sessions-title")
        with Horizontal():
            yield Input(placeholder="filter by instance id (blank = all)",
                       value=str(self._instance_id) if self._instance_id else "", id="sess-instance")
            yield Input(placeholder="search text", id="sess-query")
            yield Button("Search", id="sess-search")
        yield DataTable(id="sessions-table")
        with Horizontal():
            yield Button("View", id="sess-view")
            yield Button("Delete selected", id="sess-delete")
            yield Button("Refresh", id="sess-refresh")
        yield Label("", id="sessions-status")
        yield TextArea("", id="sess-detail", read_only=True)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(*COLUMNS)
        await self.refresh_sessions()

    async def refresh_sessions(self) -> None:
        status = self.query_one("#sessions-status", Label)
        instance_raw = self.query_one("#sess-instance", Input).value.strip()
        query = self.query_one("#sess-query", Input).value.strip() or None
        instance_id = int(instance_raw) if instance_raw else None
        try:
            self._sessions = await self.app.client.list_sessions(instance_id=instance_id, q=query)
        except ApiError as exc:
            status.update(f"Failed to load: {exc}")
            return
        status.update("" if self._sessions else "No sessions found.")
        table = self.query_one("#sessions-table", DataTable)
        table.clear()
        for s in self._sessions:
            table.add_row(str(s["id"]), str(s.get("instance_id", "")), s.get("title") or "", str(s.get("item_count", 0)),
                         key=str(s["id"]))

    def _selected(self) -> dict | None:
        table = self.query_one("#sessions-table", DataTable)
        if table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((s for s in self._sessions if str(s["id"]) == key), None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        status = self.query_one("#sessions-status", Label)
        bid = event.button.id
        if bid == "sess-search":
            await self.refresh_sessions()
        elif bid == "sess-refresh":
            await self.refresh_sessions()
        elif bid == "sess-view":
            session = self._selected()
            if session is None:
                return
            try:
                detail = await self.app.client.get_session(session["id"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            messages = detail.get("messages", [])
            text = "\n".join(f"[{m.get('role', '?')}] {m.get('content', '')}" for m in messages[-100:])
            self.query_one("#sess-detail", TextArea).text = text or "(no messages)"
        elif bid == "sess-delete":
            session = self._selected()
            if session is None:
                return
            try:
                await self.app.client.delete_session(session["id"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Deleted session {session['id']}.")
            await self.refresh_sessions()
