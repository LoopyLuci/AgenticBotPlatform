"""List-and-act screens for the areas `abp_cli` already covers: hooks, MCP servers (internal and
external), plugins, skills, snapshots, security (allowed users) and linked peers. They share one
small base (ManageScreen: a table, an add form, row actions, a confirm dialog before anything
destructive, API errors shown in the status line) and talk only to the dashboard API through the
app's DashboardClient, like every other screen. Secrets are never shown: the API already reduces
them to flags (has_auth_token) and the token/key inputs here are password fields."""

from __future__ import annotations

import json
from typing import Any, Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static, TextArea

from bot.tui.client import ApiError


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No before a destructive action."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str):
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._question, id="confirm-question")
            with Horizontal():
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


def _yn(value: Any) -> str:
    return "yes" if value else "no"


class ManageScreen(Screen):
    """Subclasses set the class attributes and implement load(), add() and an `_x` method per action."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    TITLE_TEXT = ""
    PREFIX = ""
    COLUMNS: tuple = ()          # (header, row -> value)
    FIELDS: tuple = ()           # (suffix, placeholder, secret)
    ADD_LABEL = "Add"
    ACTIONS: tuple = ()          # (suffix, label, method name, destructive question or None)

    def compose(self) -> ComposeResult:
        p = self.PREFIX
        yield Static(self.TITLE_TEXT, id=f"{p}-title")
        if self.FIELDS:
            with Horizontal(id=f"{p}-form"):
                for suffix, placeholder, secret in self.FIELDS:
                    yield Input(placeholder=placeholder, password=secret, id=f"{p}-{suffix}")
                yield Button(self.ADD_LABEL, id=f"{p}-add", variant="primary")
        yield DataTable(id=f"{p}-table")
        with Horizontal():
            for suffix, label, _method, _confirm in self.ACTIONS:
                yield Button(label, id=f"{p}-{suffix}")
            yield Button("Refresh", id=f"{p}-refresh")
        yield Label("", id=f"{p}-status")
        yield TextArea("", id=f"{p}-detail", read_only=True)
        yield Footer()

    _rows: list

    async def on_mount(self) -> None:
        self._rows = []
        table = self.query_one(f"#{self.PREFIX}-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(*(h for h, _ in self.COLUMNS))
        await self.refresh_rows()

    # ---- subclass hooks
    async def load(self) -> list:
        raise NotImplementedError

    def row_key(self, row: dict) -> str:
        return str(row.get("name", ""))

    async def add(self, values: dict) -> str:
        """Send the add form; return the status text (a leading "!" means a validation message, nothing was sent)."""
        raise NotImplementedError

    # ---- plumbing
    def _status(self, text: str) -> None:
        self.query_one(f"#{self.PREFIX}-status", Label).update(text)

    def _value(self, suffix: str) -> str:
        return self.query_one(f"#{self.PREFIX}-{suffix}", Input).value.strip()

    def _show(self, text: str) -> None:
        self.query_one(f"#{self.PREFIX}-detail", TextArea).text = text

    async def action_refresh(self) -> None:
        await self.refresh_rows()

    async def refresh_rows(self) -> None:
        try:
            self._rows = await self.load()
        except ApiError as exc:
            self._status(f"Failed to load: {exc}")
            return
        table = self.query_one(f"#{self.PREFIX}-table", DataTable)
        keep = table.cursor_row
        table.clear()
        for row in self._rows:
            table.add_row(*(str(fn(row)) for _, fn in self.COLUMNS), key=self.row_key(row))
        if keep is not None and keep < table.row_count:      # stay on the row just acted on
            table.move_cursor(row=keep)
        self._status("" if self._rows else "Nothing here yet.")

    def selected(self) -> dict | None:
        table = self.query_one(f"#{self.PREFIX}-table", DataTable)
        if table.cursor_row is None or not self._rows:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((r for r in self._rows if self.row_key(r) == key), None)

    async def _run(self, call: Callable, done: str) -> None:
        """Await an API call, show `done` or the API's error, then reload."""
        try:
            await call()
        except ApiError as exc:
            self._status(f"Failed: {exc}")
            return
        await self.refresh_rows()
        self._status(done)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        p = self.PREFIX
        if bid == f"{p}-refresh":
            await self.refresh_rows()
        elif bid == f"{p}-add":
            values = {suffix: self._value(suffix) for suffix, _, _ in self.FIELDS}
            try:
                message = await self.add(values)
            except ApiError as exc:
                self._status(f"Failed: {exc}")
                return
            if message.startswith("!"):
                self._status(message[1:])
                return
            for suffix, _, _ in self.FIELDS:      # secrets and everything else leave the inputs once sent
                self.query_one(f"#{p}-{suffix}", Input).value = ""
            await self.refresh_rows()
            self._status(message)
        else:
            for suffix, _label, method, question in self.ACTIONS:
                if bid != f"{p}-{suffix}":
                    continue
                row = self.selected()
                if row is None:
                    self._status("Select a row first.")
                    return
                handler = getattr(self, method)
                if question is None:
                    await handler(row)
                else:
                    async def _after(yes: bool | None, handler=handler, row=row) -> None:
                        if yes:
                            await handler(row)
                        else:
                            self._status("Cancelled.")
                    await self.app.push_screen(ConfirmScreen(question.format(key=self.row_key(row))), _after)
                return


class HooksScreen(ManageScreen):
    TITLE_TEXT = "Hooks"
    PREFIX = "hooks"
    COLUMNS = (("id", lambda r: r["id"]), ("event", lambda r: r.get("event", "")),
               ("matcher", lambda r: r.get("matcher") or ""), ("command", lambda r: r.get("command", "")),
               ("enabled", lambda r: _yn(r.get("enabled"))))
    FIELDS = (("event", "event (e.g. PreToolUse)", False), ("command", "command", False),
              ("matcher", "matcher (optional)", False))
    ACTIONS = (("enable", "Enable", "_enable", None), ("disable", "Disable", "_disable", None),
               ("remove", "Remove", "_remove", "Remove hook {key}?"))

    async def load(self) -> list:
        return await self.app.client.list_hooks()

    def row_key(self, row: dict) -> str:
        return str(row["id"])

    async def add(self, values: dict) -> str:
        if not values["event"] or not values["command"]:
            return "!Event and command are both required."
        await self.app.client.add_hook(values["event"], values["command"], matcher=values["matcher"] or None)
        return f"Added a {values['event']} hook."

    async def _enable(self, row: dict) -> None:
        await self._run(lambda: self.app.client.enable_hook(row["id"]), f"Enabled hook {row['id']}.")

    async def _disable(self, row: dict) -> None:
        await self._run(lambda: self.app.client.disable_hook(row["id"]), f"Disabled hook {row['id']}.")

    async def _remove(self, row: dict) -> None:
        await self._run(lambda: self.app.client.delete_hook(row["id"]), f"Removed hook {row['id']}.")


class McpScreen(ManageScreen):
    """Internal (Claude Desktop config) and external (the agent's own) MCP servers in one list."""

    TITLE_TEXT = "MCP servers (internal and external)"
    PREFIX = "mcp"
    COLUMNS = (("kind", lambda r: r["kind"]), ("name", lambda r: r["name"]),
               ("transport/command", lambda r: r.get("transport") or r.get("command") or ""),
               ("url", lambda r: r.get("url") or ""), ("enabled", lambda r: _yn(r.get("enabled"))),
               ("connected", lambda r: _yn(r.get("connected")) if r["kind"] == "external" else "-"),
               ("token", lambda r: "set" if r.get("has_auth_token") else ""))
    FIELDS = (("name", "external server name", False), ("transport", "stdio or remote", False),
              ("command", "command (stdio)", False), ("args", 'args as JSON list, e.g. ["-m", "srv"]', False),
              ("url", "url (remote)", False), ("token", "auth token (optional)", True))
    ADD_LABEL = "Add external"
    ACTIONS = (("enable", "Enable", "_enable", None), ("disable", "Disable", "_disable", None),
               ("remove", "Remove external", "_remove", "Remove external MCP server {key}?"),
               ("logs", "Logs (internal)", "_logs", None))

    async def load(self) -> list:
        internal = await self.app.client.list_mcp_servers()
        external = await self.app.client.list_external_mcp_servers()
        return [{**s, "kind": "internal"} for s in internal] + [{**s, "kind": "external"} for s in external]

    def row_key(self, row: dict) -> str:
        return f"{row['kind']}:{row['name']}"

    async def add(self, values: dict) -> str:
        if not values["name"] or values["transport"] not in ("stdio", "remote"):
            return "!A name and a transport (stdio or remote) are required."
        args = None
        if values["args"]:
            try:
                args = json.loads(values["args"])
            except ValueError:
                return "!Args must be a JSON list."
            if not isinstance(args, list):
                return "!Args must be a JSON list."
        await self.app.client.add_external_mcp_server(
            values["name"], values["transport"], command=values["command"] or None, args=args,
            url=values["url"] or None, auth_token=values["token"] or None)
        return f"Added external server {values['name']!r}."

    async def _toggle(self, row: dict, on: bool) -> None:
        c = self.app.client
        if row["kind"] == "external":
            method = c.enable_external_mcp_server if on else c.disable_external_mcp_server
        else:
            method = c.enable_mcp_server if on else c.disable_mcp_server
        await self._run(lambda: method(row["name"]), f"{'Enabled' if on else 'Disabled'} {row['name']}.")

    async def _enable(self, row: dict) -> None:
        await self._toggle(row, True)

    async def _disable(self, row: dict) -> None:
        await self._toggle(row, False)

    async def _remove(self, row: dict) -> None:
        if row["kind"] != "external":
            self._status("Only external servers can be removed here; disable an internal one instead.")
            return
        await self._run(lambda: self.app.client.remove_external_mcp_server(row["name"]), f"Removed {row['name']}.")

    async def _logs(self, row: dict) -> None:
        if row["kind"] != "internal":
            self._status("Logs are kept for internal servers only.")
            return
        try:
            lines = await self.app.client.mcp_server_logs(row["name"])
        except ApiError as exc:
            self._status(f"Failed: {exc}")
            return
        self._show("\n".join(lines) or "(no log lines)")


class PluginsScreen(ManageScreen):
    TITLE_TEXT = "Plugins"
    PREFIX = "plugins"
    COLUMNS = (("name", lambda r: r["name"]), ("version", lambda r: r.get("version") or ""),
               ("enabled", lambda r: _yn(r.get("enabled"))),
               ("tools", lambda r: ", ".join(r.get("tools") or [])),
               ("description", lambda r: r.get("description") or ""))
    FIELDS = (("path", "path of a plugin file to install", False),)
    ADD_LABEL = "Install"
    ACTIONS = (("enable", "Enable", "_enable", None), ("disable", "Disable", "_disable", None),
               ("remove", "Remove", "_remove", "Remove plugin {key}?"))

    async def load(self) -> list:
        return await self.app.client.list_plugins()

    async def add(self, values: dict) -> str:
        if not values["path"]:
            return "!A path is required."
        await self.app.client.install_plugin(values["path"])
        return "Installed."

    async def _enable(self, row: dict) -> None:
        await self._run(lambda: self.app.client.enable_plugin(row["name"]), f"Enabled {row['name']}.")

    async def _disable(self, row: dict) -> None:
        await self._run(lambda: self.app.client.disable_plugin(row["name"]), f"Disabled {row['name']}.")

    async def _remove(self, row: dict) -> None:
        await self._run(lambda: self.app.client.delete_plugin(row["name"]), f"Removed {row['name']}.")


class SkillsScreen(ManageScreen):
    TITLE_TEXT = "Skills (global ones; give an instance id to add one for a single bot)"
    PREFIX = "skills"
    COLUMNS = (("name", lambda r: r["name"]),
               ("instance", lambda r: r["instance_id"] if r.get("instance_id") is not None else "global"),
               ("description", lambda r: r.get("description") or ""))
    FIELDS = (("instance", "instance id (blank = global)", False), ("name", "name", False),
              ("description", "description", False), ("content", "content (one line)", False))
    ACTIONS = (("remove", "Remove", "_remove", "Remove skill {key}?"),)

    async def load(self) -> list:
        return await self.app.client.list_skills()

    def row_key(self, row: dict) -> str:
        return f"{row.get('instance_id')}:{row['name']}"

    async def add(self, values: dict) -> str:
        if not values["name"] or not values["content"]:
            return "!Name and content are both required."
        instance = values["instance"]
        if instance and not instance.isdigit():
            return "!Instance id must be a number."
        await self.app.client.create_skill(int(instance) if instance else None, values["name"],
                                           values["description"], values["content"], is_global=not instance)
        return f"Added skill {values['name']!r}."

    async def _remove(self, row: dict) -> None:
        await self._run(lambda: self.app.client.delete_skill(row["name"], instance_id=row.get("instance_id")),
                        f"Removed {row['name']}.")


class SnapshotsScreen(ManageScreen):
    TITLE_TEXT = "Snapshots (config and database)"
    PREFIX = "snapshots"
    COLUMNS = (("name", lambda r: r["name"]), ("label", lambda r: r.get("label") or ""),
               ("created", lambda r: r.get("created_at") or ""))
    FIELDS = (("label", "label (optional)", False),)
    ADD_LABEL = "Create"
    ACTIONS = (("restore", "Restore", "_restore", "Restore snapshot {key}? The current config and database are replaced."),
               ("delete", "Delete", "_delete", "Delete snapshot {key}?"))

    async def load(self) -> list:
        return await self.app.client.list_snapshots()

    async def add(self, values: dict) -> str:
        made = await self.app.client.create_snapshot(label=values["label"] or None)
        return f"Created snapshot {made.get('name', '')}."

    async def _restore(self, row: dict) -> None:
        await self._run(lambda: self.app.client.restore_snapshot(row["name"]), f"Restored {row['name']}.")

    async def _delete(self, row: dict) -> None:
        await self._run(lambda: self.app.client.delete_snapshot(row["name"]), f"Deleted {row['name']}.")


class SecurityScreen(ManageScreen):
    TITLE_TEXT = "Security: allowed users"
    PREFIX = "security"
    COLUMNS = (("telegram id", lambda r: r["telegram_id"]), ("name", lambda r: r.get("name") or ""),
               ("added", lambda r: r.get("added_at") or ""))
    FIELDS = (("telegram_id", "telegram id", False), ("name", "name (optional)", False))
    ACTIONS = (("remove", "Remove", "_remove", "Remove allowed user {key}?"),)

    async def load(self) -> list:
        return await self.app.client.list_allowed_users()

    def row_key(self, row: dict) -> str:
        return str(row["telegram_id"])

    async def add(self, values: dict) -> str:
        if not values["telegram_id"].isdigit():
            return "!A numeric telegram id is required."
        await self.app.client.add_allowed_user(values["telegram_id"], name=values["name"] or None)
        return f"Added {values['telegram_id']}."

    async def _remove(self, row: dict) -> None:
        await self._run(lambda: self.app.client.remove_allowed_user(str(row["telegram_id"])),
                        f"Removed {row['telegram_id']}.")


class PeersScreen(ManageScreen):
    TITLE_TEXT = "Linked servers (peers)"
    PREFIX = "peers"
    COLUMNS = (("id", lambda r: r["id"]), ("name", lambda r: r.get("name") or ""),
               ("base url", lambda r: r.get("base_url") or ""), ("last seen", lambda r: r.get("last_seen_at") or ""))
    FIELDS = (("name", "name for the peer", False), ("token", "pairing token from the peer", True),
              ("myurl", "this server's URL (optional)", False))
    ADD_LABEL = "Link"
    ACTIONS = (("overview", "Overview", "_overview", None), ("bots", "Bots", "_bots", None),
               ("remove", "Unlink", "_remove", "Unlink peer {key}?"))

    async def load(self) -> list:
        return await self.app.client.list_peers()

    def row_key(self, row: dict) -> str:
        return str(row["id"])

    async def add(self, values: dict) -> str:
        if not values["name"] or not values["token"]:
            return "!A name and the peer's pairing token are both required."
        await self.app.client.link_peer(values["name"], values["token"], my_base_url=values["myurl"] or None)
        return f"Linked {values['name']!r}."

    async def _show_call(self, call: Callable) -> None:
        try:
            data = await call()
        except ApiError as exc:
            self._status(f"Failed: {exc}")
            return
        self._show(json.dumps(data, indent=1))

    async def _overview(self, row: dict) -> None:
        await self._show_call(lambda: self.app.client.peer_overview(row["id"]))

    async def _bots(self, row: dict) -> None:
        await self._show_call(lambda: self.app.client.peer_bots(row["id"]))

    async def _remove(self, row: dict) -> None:
        await self._run(lambda: self.app.client.remove_peer(row["id"]), f"Unlinked {row.get('name') or row['id']}.")
