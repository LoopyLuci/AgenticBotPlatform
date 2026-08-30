"""Full edit view for one existing bot instance — every field the
dashboard's Bots tab can edit, plus its schedules (bot/scheduler.py),
in one screen."""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Select, Static, TextArea

from bot.tui.client import ApiError
from bot.tui.screens.add_bot import BACKENDS


class BotDetailScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, bot: dict):
        super().__init__()
        self.bot = bot

    def compose(self) -> ComposeResult:
        b = self.bot
        yield Static(f"Editing: {b['name']} ({b['platform']}/{b['backend']})", id="title")
        with VerticalScroll(id="bot-detail-form"):
            yield Label("Name", classes="field-label")
            yield Input(value=b["name"], id="field-name")
            yield Label("Backend", classes="field-label")
            yield Select([(x, x) for x in BACKENDS], value=b["backend"], id="field-backend")
            yield Label("Model override (blank = default)", classes="field-label")
            yield Input(value=b.get("model") or "", id="field-model")
            yield Label("Allowed user ID(s), comma-separated", classes="field-label")
            yield Input(value=", ".join(str(x) for x in b.get("allowed_user_ids") or []), id="field-allowed")
            yield Label("Admin user ID(s), comma-separated", classes="field-label")
            yield Input(value=", ".join(str(x) for x in b.get("admin_user_ids") or []), id="field-admins")
            yield Checkbox("Enabled", value=bool(b.get("enabled")), id="field-enabled")
            yield Label("Custom instructions (optional)", classes="field-label")
            yield TextArea(b.get("custom_instructions") or "", id="field-instructions")
            yield Label("Advanced: backend overrides (raw JSON)", classes="field-label")
            yield TextArea(json.dumps(b.get("action_overrides") or {}, indent=2), id="field-overrides")
            yield Button("Save changes", id="submit", variant="primary")
            yield Label("", id="detail-status", classes="status-line")

            yield Static("Schedules", id="schedules-title")
            yield DataTable(id="schedules-table")
            with Horizontal():
                yield Input(placeholder="chat ID", id="sched-chatid")
                yield Select([("cron", "cron"), ("loop", "loop"), ("heartbeat", "heartbeat")], value="cron", id="sched-kind")
                yield Input(placeholder="interval e.g. 10m", id="sched-interval")
                yield Input(placeholder="prompt", id="sched-prompt")
                yield Button("Add schedule", id="sched-add")
            yield Label("", id="sched-status", classes="status-line")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#schedules-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "chat", "kind", "prompt", "interval", "enabled")
        await self.refresh_schedules()

    async def refresh_schedules(self) -> None:
        table = self.query_one("#schedules-table", DataTable)
        table.clear()
        try:
            rows = await self.app.client.list_schedules(self.bot["id"])
        except ApiError:
            return
        self._schedules = rows
        for r in rows:
            table.add_row(
                str(r["id"]), str(r["chat_id"]), r["kind"], r["prompt"], f'{r["interval_s"]}s',
                "yes" if r["enabled"] else "no", key=str(r["id"]),
            )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            await self._save()
        elif event.button.id == "sched-add":
            await self._add_schedule()

    async def _save(self) -> None:
        status = self.query_one("#detail-status", Label)
        is_string_id = self.bot["platform"] in ("slack", "matrix", "whatsapp")
        allowed_raw = [s.strip() for s in self.query_one("#field-allowed", Input).value.split(",") if s.strip()]
        admin_raw = [s.strip() for s in self.query_one("#field-admins", Input).value.split(",") if s.strip()]
        try:
            action_overrides = json.loads(self.query_one("#field-overrides", TextArea).text or "{}")
        except json.JSONDecodeError as exc:
            status.update(f"Backend overrides isn't valid JSON — {exc}")
            return
        payload = {
            "name": self.query_one("#field-name", Input).value.strip(),
            "backend": str(self.query_one("#field-backend", Select).value),
            "model": self.query_one("#field-model", Input).value.strip() or None,
            "allowed_user_ids": allowed_raw if is_string_id else [int(s) for s in allowed_raw],
            "admin_user_ids": admin_raw if is_string_id else [int(s) for s in admin_raw],
            "enabled": self.query_one("#field-enabled", Checkbox).value,
            "custom_instructions": self.query_one("#field-instructions", TextArea).text.strip() or None,
            "action_overrides": action_overrides,
        }
        status.update("Saving…")
        try:
            await self.app.client.update_bot(self.bot["id"], payload)
        except ApiError as exc:
            status.update(f"Failed: {exc}")
            return
        status.update("Saved.")

    async def _add_schedule(self) -> None:
        status = self.query_one("#sched-status", Label)
        chat_id = self.query_one("#sched-chatid", Input).value.strip()
        interval = self.query_one("#sched-interval", Input).value.strip()
        prompt = self.query_one("#sched-prompt", Input).value.strip()
        if not chat_id or not interval or not prompt:
            status.update("Chat ID, interval, and prompt are all required.")
            return
        payload = {"chat_id": chat_id, "kind": str(self.query_one("#sched-kind", Select).value), "interval": interval, "prompt": prompt}
        try:
            await self.app.client.create_schedule(self.bot["id"], payload)
        except ApiError as exc:
            status.update(f"Failed: {exc}")
            return
        status.update("Added.")
        self.query_one("#sched-chatid", Input).value = ""
        self.query_one("#sched-interval", Input).value = ""
        self.query_one("#sched-prompt", Input).value = ""
        await self.refresh_schedules()
