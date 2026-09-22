"""The ~65 native_agent.* settings (ABP Agents page in the dashboard/desktop app) —
**schema-driven**, like the GUI itself: built from GET /api/agent/config/schema rather than
hand-ported field by field, so it stays correct as settings are added later. One long
scrollable form grouped by tab (Runtime/Safety/Tools/Sub-agents & swarms/Models), not the
GUI's separate tab pages — a real, disclosed scope difference (see docs/agents/cli-tui.md),
not a hidden one.

Also doubles as a specific bot's OWN agent settings (worker model/effort, fallback,
permission mode, plan approval) when opened with an instance_id — those aren't part of the
native_agent.* schema (they live in bot/agent_settings.py, a separate table/route), so they
get their own small section instead of pretending to be schema fields.
"""

from __future__ import annotations

from typing import Any, Optional

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, Select, Static, TextArea

from bot.tui.client import ApiError

AGENT_SETTINGS_FIELDS = ("permission_mode", "max_children", "worker_model", "worker_effort",
                         "manager_effort", "fallback_provider", "fallback_model", "require_plan_approval")


def _widget_id(field_id: str) -> str:
    return "f-" + field_id.replace(".", "-")


class AgentSettingsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, instance_id: Optional[int] = None):
        super().__init__()
        self._instance_id = instance_id
        self._schema: dict = {}
        self._values: dict = {}
        self._own: dict = {}

    def compose(self) -> ComposeResult:
        title = f"Agent settings — bot #{self._instance_id}" if self._instance_id else "Agent settings — global defaults"
        yield Static(title, id="as-title")
        with VerticalScroll(id="as-form"):
            yield Label("Loading…", id="as-loading")
        yield Label("", id="as-status")
        with Vertical(id="as-actions"):
            yield Button("Save changes", id="as-save", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        status = self.query_one("#as-status", Label)
        try:
            self._schema = await self.app.client.get_agent_config_schema()
            self._values = (await self.app.client.get_agent_config())["values"]
            if self._instance_id is not None:
                self._own = await self.app.client.get_agent_settings(self._instance_id, own=True)
        except ApiError as exc:
            status.update(f"Failed to load: {exc}")
            return
        self._build_form()

    def _build_form(self) -> None:
        form = self.query_one("#as-form", VerticalScroll)
        form.remove_children()
        widgets = []
        if self._instance_id is not None:
            widgets.append(Static("This bot's own settings (blank = follow the global default)", classes="as-section"))
            widgets.append(Label("Permission mode (blank = default)", classes="field-label"))
            widgets.append(Input(value=self._own.get("permission_mode") or "", id="f-own-permission_mode"))
            widgets.append(Label("Sub-agents at once", classes="field-label"))
            widgets.append(Input(value=str(self._own.get("max_children") or ""), id="f-own-max_children"))
            widgets.append(Label("Sub-agent model (provider/model)", classes="field-label"))
            widgets.append(Input(value=self._own.get("worker_model") or "", id="f-own-worker_model"))
            widgets.append(Label("Sub-agent effort", classes="field-label"))
            widgets.append(Input(value=self._own.get("worker_effort") or "", id="f-own-worker_effort"))
            widgets.append(Label("Manager effort", classes="field-label"))
            widgets.append(Input(value=self._own.get("manager_effort") or "", id="f-own-manager_effort"))
            widgets.append(Label("Fallback provider", classes="field-label"))
            widgets.append(Input(value=self._own.get("fallback_provider") or "", id="f-own-fallback_provider"))
            widgets.append(Label("Fallback model", classes="field-label"))
            widgets.append(Input(value=self._own.get("fallback_model") or "", id="f-own-fallback_model"))
            widgets.append(Checkbox("Ask me to approve a plan before sub-agents start",
                                    bool(self._own.get("require_plan_approval")), id="f-own-require_plan_approval"))

        tabs = {t["id"]: t["title"] for t in self._schema.get("tabs", [])}
        current_tab = None
        for field in self._schema.get("fields", []):
            if field["tab"] != current_tab:
                current_tab = field["tab"]
                widgets.append(Static(tabs.get(current_tab, current_tab), classes="as-section"))
            widgets.append(Label(field["label"], classes="field-label"))
            widgets.append(self._field_widget(field))
        if not widgets:
            widgets.append(Label("No settings returned by the schema."))
        form.mount_all(widgets)

    def _field_widget(self, field: dict):
        fid = _widget_id(field["id"])
        value = self._values.get(field["id"])
        ftype = field["type"]
        if ftype == "bool":
            return Checkbox(field["label"], bool(value), id=fid)
        if ftype == "enum" and field.get("choices"):
            options = [(label, val) for val, label in field["choices"]]
            return Select(options, value=value if value is not None else Select.BLANK, id=fid)
        if ftype in ("list", "rules"):
            text = "\n".join(value) if isinstance(value, list) else (value or "")
            return TextArea(text, id=fid)
        if ftype == "textarea":
            return TextArea(str(value or ""), id=fid)
        return Input(value="" if value is None else str(value), id=fid)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "as-save":
            return
        status = self.query_one("#as-status", Label)
        status.update("Saving…")
        changes: dict[str, Any] = {}
        for field in self._schema.get("fields", []):
            fid = _widget_id(field["id"])
            try:
                widget = self.query_one(f"#{fid}")
            except Exception:
                continue
            changes[field["id"]] = self._read_widget(field, widget)
        try:
            if changes:
                await self.app.client.set_agent_config(changes)
            if self._instance_id is not None:
                own_fields = self._read_own_fields()
                await self.app.client.set_agent_settings(self._instance_id, **own_fields)
        except ApiError as exc:
            status.update(f"Failed: {exc}")
            return
        status.update("Saved.")

    def _read_widget(self, field: dict, widget) -> Any:
        ftype = field["type"]
        if ftype == "bool":
            return bool(widget.value)
        if ftype == "enum":
            return None if widget.value is Select.BLANK else widget.value
        if ftype in ("list", "rules"):
            return [line.strip() for line in widget.text.split("\n") if line.strip()]
        if ftype == "textarea":
            return widget.text
        if ftype == "int":
            text = widget.value.strip()
            return int(text) if text else None
        if ftype == "float":
            text = widget.value.strip()
            return float(text) if text else None
        return widget.value

    def _read_own_fields(self) -> dict:
        def _text(field_id: str) -> Optional[str]:
            v = self.query_one(f"#f-own-{field_id}", Input).value.strip()
            return v or None

        out: dict[str, Any] = {
            "permission_mode": _text("permission_mode"),
            "worker_model": _text("worker_model"),
            "worker_effort": _text("worker_effort"),
            "manager_effort": _text("manager_effort"),
            "fallback_provider": _text("fallback_provider"),
            "fallback_model": _text("fallback_model"),
            "require_plan_approval": self.query_one("#f-own-require_plan_approval", Checkbox).value,
        }
        max_children = _text("max_children")
        out["max_children"] = int(max_children) if max_children else None
        return out
