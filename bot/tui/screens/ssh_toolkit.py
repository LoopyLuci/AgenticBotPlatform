"""SSH Toolkit (github.com/LoopyLuci/SSH_Toolkit) - a separately maintained connection
manager, vendored as a git submodule at vendor/ssh_toolkit and reached only through
bot/dashboard_client.py's ssh_toolkit_* methods (which call the dashboard's own
/api/ssh-toolkit/* routes, backed by bot/ssh_toolkit.py). List/add/remove/test
connections, and check/apply an update to the vendored toolkit itself."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from bot.tui.client import ApiError

COLUMNS = ("Name", "HostName", "Port", "User", "Tags")


class SshToolkitScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("SSH Toolkit", id="ssh-title")
        yield Label("", id="ssh-availability")
        with Vertical(id="ssh-form"):
            with Horizontal():
                yield Input(placeholder="name", id="ssh-name")
                yield Input(placeholder="host or IP", id="ssh-hostname")
                yield Input(placeholder="user (optional)", id="ssh-user")
                yield Button("Add", id="ssh-add", variant="primary")
        yield DataTable(id="ssh-table")
        with Horizontal():
            yield Button("Test selected", id="ssh-test")
            yield Button("Remove selected", id="ssh-remove")
            yield Button("Refresh", id="ssh-refresh")
            yield Button("Check for a toolkit update", id="ssh-check-update")
            yield Button("Apply update", id="ssh-apply-update")
        yield Label("", id="ssh-status")
        yield Footer()

    _fetch_generation = 0

    async def on_mount(self) -> None:
        table = self.query_one("#ssh-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(*COLUMNS)
        await self.refresh_all()

    async def refresh_all(self) -> None:
        self._fetch_generation += 1
        generation = self._fetch_generation
        status = self.query_one("#ssh-status", Label)
        availability = self.query_one("#ssh-availability", Label)
        try:
            avail = await self.app.client.ssh_toolkit_status()
        except ApiError as exc:
            if generation != self._fetch_generation:
                return
            availability.update(f"Couldn't reach the SSH Toolkit route: {exc}")
            return
        if generation != self._fetch_generation:
            return
        if not avail.get("available"):
            availability.update(f"SSH Toolkit is not available: {avail.get('reason', '')}")
            self._connections = []
            self._render_table()
            return
        availability.update("")
        try:
            connections = await self.app.client.ssh_toolkit_connections()
        except ApiError as exc:
            if generation != self._fetch_generation:
                return
            status.update(f"Failed to load connections: {exc}")
            self._connections = []
            self._render_table()
            return
        if generation != self._fetch_generation:
            return
        self._connections = connections
        self._render_table()

    def _render_table(self) -> None:
        table = self.query_one("#ssh-table", DataTable)
        table.clear()
        for c in self._connections:
            table.add_row(c.get("Name", ""), c.get("HostName", ""), str(c.get("Port", "")),
                         c.get("User", "") or "", c.get("Tags", "") or "", key=c.get("Name", ""))

    def _selected(self) -> dict | None:
        table = self.query_one("#ssh-table", DataTable)
        if table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((c for c in self._connections if c.get("Name") == key), None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        status = self.query_one("#ssh-status", Label)
        bid = event.button.id
        if bid == "ssh-add":
            name = self.query_one("#ssh-name", Input).value.strip()
            host_name = self.query_one("#ssh-hostname", Input).value.strip()
            user = self.query_one("#ssh-user", Input).value.strip() or None
            if not name or not host_name:
                status.update("Name and host are both required.")
                return
            try:
                await self.app.client.ssh_toolkit_add_connection(name, host_name, user=user)
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Added {name!r}.")
            await self.refresh_all()
        elif bid == "ssh-test":
            conn = self._selected()
            if conn is None:
                return
            try:
                result = await self.app.client.ssh_toolkit_test_connection(conn["Name"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"{conn['Name']}: {'reachable' if result.get('reachable') else 'not reachable'}")
        elif bid == "ssh-remove":
            conn = self._selected()
            if conn is None:
                return
            try:
                await self.app.client.ssh_toolkit_remove_connection(conn["Name"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Removed {conn['Name']!r}.")
            await self.refresh_all()
        elif bid == "ssh-refresh":
            await self.refresh_all()
        elif bid == "ssh-check-update":
            try:
                check = await self.app.client.ssh_toolkit_check_update()
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            if check.get("Error"):
                status.update(f"Couldn't check: {check['Error']}")
            elif check.get("UpdateAvailable"):
                status.update(f"Update available: {check['InstalledVersion']} -> {check['LatestVersion']}")
            else:
                status.update(f"Up to date ({check.get('InstalledVersion')}).")
        elif bid == "ssh-apply-update":
            try:
                result = await self.app.client.ssh_toolkit_apply_update()
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Updated to {result['Version']}." if result.get("Updated") else f"Not updated: {result.get('Reason')}.")
