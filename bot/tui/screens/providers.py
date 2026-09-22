"""Named model providers (config/providers.yaml) — the TUI's counterpart to the dashboard's
Models page provider management: list, add, remove, the deleted-providers store with
restore, and browsing one provider's models."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from bot.tui.client import ApiError

COLUMNS = ("name", "base_url", "protocol")
DELETED_COLUMNS = ("name", "base_url", "deleted_at")


class ProvidersScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("Providers", id="providers-title")
        with Vertical(id="providers-form"):
            with Horizontal():
                yield Input(placeholder="name", id="prov-name")
                yield Input(placeholder="base URL, e.g. https://openrouter.ai/api/v1", id="prov-baseurl")
                yield Input(placeholder="protocol (default openai)", id="prov-protocol")
            with Horizontal():
                yield Input(placeholder="API key (optional — or set api_key_env below)", password=True, id="prov-apikey")
                yield Input(placeholder="API key env var (optional)", id="prov-apikeyenv")
                yield Button("Add / update", id="prov-add", variant="primary")
        yield DataTable(id="providers-table")
        with Horizontal():
            yield Button("Remove selected", id="prov-remove")
            yield Button("Browse models", id="prov-models")
            yield Button("Refresh", id="prov-refresh")
        yield Label("", id="providers-status")
        yield Static("Deleted providers", id="deleted-title")
        yield DataTable(id="deleted-table")
        yield Button("Restore selected", id="prov-restore")
        yield Static("Models (select a provider above, then Browse models)", id="models-title")
        yield DataTable(id="providers-models-table")
        yield Footer()

    async def on_mount(self) -> None:
        for table_id, cols in (("providers-table", COLUMNS), ("deleted-table", DELETED_COLUMNS),
                                ("providers-models-table", ("id", "free", "context"))):
            table = self.query_one(f"#{table_id}", DataTable)
            table.cursor_type = "row"
            table.add_columns(*cols)
        await self.refresh_all()

    async def refresh_all(self) -> None:
        status = self.query_one("#providers-status", Label)
        try:
            self._providers = await self.app.client.list_providers()
            self._deleted = await self.app.client.provider_store(status="deleted")
        except ApiError as exc:
            status.update(f"Failed to load: {exc}")
            return
        status.update("")
        table = self.query_one("#providers-table", DataTable)
        table.clear()
        for p in self._providers:
            table.add_row(p["name"], p.get("base_url", ""), p.get("protocol", "openai"), key=p["name"])
        dtable = self.query_one("#deleted-table", DataTable)
        dtable.clear()
        for p in self._deleted:
            dtable.add_row(p.get("name", ""), p.get("base_url", ""), p.get("deleted_at", ""), key=p.get("name", ""))

    def _selected(self, table_id: str, rows: list[dict]) -> dict | None:
        table = self.query_one(f"#{table_id}", DataTable)
        if table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((r for r in rows if r.get("name") == key), None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        status = self.query_one("#providers-status", Label)
        bid = event.button.id
        if bid == "prov-add":
            name = self.query_one("#prov-name", Input).value.strip()
            base_url = self.query_one("#prov-baseurl", Input).value.strip()
            protocol = self.query_one("#prov-protocol", Input).value.strip() or "openai"
            api_key = self.query_one("#prov-apikey", Input).value.strip() or None
            api_key_env = self.query_one("#prov-apikeyenv", Input).value.strip() or None
            if not name or not base_url:
                status.update("Name and base URL are both required.")
                return
            try:
                await self.app.client.set_provider(name, base_url, protocol=protocol, api_key=api_key, api_key_env=api_key_env)
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Saved {name!r}.")
            await self.refresh_all()
        elif bid == "prov-remove":
            provider = self._selected("providers-table", self._providers)
            if provider is None:
                return
            try:
                await self.app.client.delete_provider(provider["name"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Removed {provider['name']!r} (kept in the deleted-providers store).")
            await self.refresh_all()
        elif bid == "prov-restore":
            provider = self._selected("deleted-table", self._deleted)
            if provider is None:
                return
            try:
                await self.app.client.restore_provider(provider["name"])
            except ApiError as exc:
                status.update(f"Failed: {exc}")
                return
            status.update(f"Restored {provider['name']!r}.")
            await self.refresh_all()
        elif bid == "prov-models":
            provider = self._selected("providers-table", self._providers)
            if provider is None:
                return
            mtable = self.query_one("#providers-models-table", DataTable)
            mtable.clear()
            try:
                models = await self.app.client.provider_models(provider["name"])
            except ApiError as exc:
                status.update(f"Failed to browse models: {exc}")
                return
            for m in models[:200]:
                mtable.add_row(str(m.get("id", "")), "yes" if m.get("free") else "no", str(m.get("context") or ""))
            status.update(f"{len(models)} model(s) for {provider['name']!r}.")
        elif bid == "prov-refresh":
            await self.refresh_all()
