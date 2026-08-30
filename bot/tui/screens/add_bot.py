"""The 5-platform bot-creation wizard — the TUI's counterpart to the
dashboard's Add-a-bot form, driven by the exact same server-side data
(GET /api/platform-guides for help text, POST /api/validate-field for
live per-field validation) so the two forms share real behavior, not
just a similar layout.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Select, Static

from bot.tui.client import ApiError

PLATFORMS = ("telegram", "discord", "slack", "matrix", "whatsapp")
BACKENDS = ("cli", "api", "ui", "hermes_cli", "hermes_gateway", "custom_model")


class AddBotScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Static("Add a bot", id="title")
        with VerticalScroll(id="add-bot-form"):
            yield Label("Name", classes="field-label")
            yield Input(placeholder="e.g. claude-support-telegram", id="field-name")
            yield Label("Platform", classes="field-label")
            yield Select([(p, p) for p in PLATFORMS], value="telegram", id="field-platform")
            yield Label("Backend", classes="field-label")
            yield Select([(b, b) for b in BACKENDS], value="cli", id="field-backend")
            yield Vertical(id="cred-fields")
            yield Label("Allowed user ID(s), comma-separated", classes="field-label")
            yield Input(id="field-allowed")
            yield Label("Admin user ID(s), comma-separated (optional)", classes="field-label")
            yield Input(id="field-admins")
            yield Button("Add bot", id="submit", variant="primary")
            yield Label("", id="add-bot-status", classes="status-line")
        yield Footer()

    async def on_mount(self) -> None:
        try:
            self._guides = await self.app.client.platform_guides()
        except ApiError:
            self._guides = {}
        await self._rebuild_credential_fields("telegram")

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-platform":
            await self._rebuild_credential_fields(str(event.value))

    async def _rebuild_credential_fields(self, platform: str) -> None:
        container = self.query_one("#cred-fields", Vertical)
        await container.remove_children()
        guide = self._guides.get(platform, {"fields": {}})
        widgets = []
        for field_name, meta in guide["fields"].items():
            widgets.append(Label(meta.get("label", field_name), classes="field-label"))
            widgets.append(Input(id=f"cred-{field_name}", password="token" in field_name or "secret" in field_name))
            if meta.get("help"):
                widgets.append(Label(meta["help"], classes="field-help"))
        if widgets:
            await container.mount_all(widgets)

    async def on_input_blurred(self, event: Input.Blurred) -> None:
        if not event.input.id or not event.input.id.startswith("cred-"):
            return
        field = event.input.id[len("cred-") :]
        value = event.input.value.strip()
        if not value:
            return
        platform = self.query_one("#field-platform", Select).value
        guide = self._guides.get(str(platform), {"fields": {}})
        if field not in guide["fields"]:
            return
        try:
            result = await self.app.client.validate_field(str(platform), field, value)
        except ApiError:
            return
        event.input.styles.border = ("solid", "green" if result["ok"] else "red")

    def _collect_credentials(self, platform: str) -> dict:
        guide = self._guides.get(platform, {"fields": {}})
        creds = {}
        for field_name in guide["fields"]:
            try:
                value = self.query_one(f"#cred-{field_name}", Input).value.strip()
            except Exception:
                continue
            if value:
                creds[field_name] = value
        return creds

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit":
            return
        status = self.query_one("#add-bot-status", Label)
        platform = str(self.query_one("#field-platform", Select).value)
        is_string_id = platform in ("slack", "matrix", "whatsapp")
        allowed_raw = [s.strip() for s in self.query_one("#field-allowed", Input).value.split(",") if s.strip()]
        admin_raw = [s.strip() for s in self.query_one("#field-admins", Input).value.split(",") if s.strip()]
        payload = {
            "name": self.query_one("#field-name", Input).value.strip(),
            "platform": platform,
            "backend": str(self.query_one("#field-backend", Select).value),
            "credentials": self._collect_credentials(platform),
            "allowed_user_ids": allowed_raw if is_string_id else [int(s) for s in allowed_raw],
            "admin_user_ids": admin_raw if is_string_id else [int(s) for s in admin_raw],
        }
        if not payload["name"]:
            status.update("Name is required.")
            return
        status.update("Adding…")
        try:
            await self.app.client.create_bot(payload)
        except ApiError as exc:
            status.update(f"Failed: {exc}")
            return
        self.app.pop_screen()
