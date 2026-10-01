"""The ABP File Server in the terminal (the dashboard's /api/fileserver, like the Storage page and `abp nas`): the server,
the parity array, shares, drives and their failure risk, alerts; start/stop the server, sync and scrub parity, run the
mover and the index, search the shares."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Log, Static

from bot.tui.client import ApiError

A = "/api/fileserver"


class StorageScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Static("Storage", id="st-title")
        yield Label("", id="st-server")
        yield Label("", id="st-array")
        yield DataTable(id="st-disks")
        yield DataTable(id="st-shares")
        with Horizontal():
            yield Button("Start/stop server", id="st-srv", variant="primary")
            yield Button("Sync parity", id="st-sync")
            yield Button("Scrub 10%", id="st-scrub")
            yield Button("Mover", id="st-mover")
            yield Button("Index", id="st-index")
            yield Button("Drive health", id="st-health")
            yield Button("Refresh (r)", id="st-refresh")
        with Horizontal():
            yield Input(placeholder="search the shares (words or meaning)", id="st-q")
            yield Button("Search", id="st-search")
        yield Log(id="st-log", max_lines=500)
        yield Label("", id="st-status")
        yield Footer()

    async def on_mount(self) -> None:
        d = self.query_one("#st-disks", DataTable)
        d.add_columns("Disk", "State", "Files", "Protected", "Path")
        s = self.query_one("#st-shares", DataTable)
        s.add_columns("Share", "Access", "Cache / folder", "Comment")
        await self.action_refresh()

    def _log(self, text: str) -> None:
        self.query_one("#st-log", Log).write_line(text)

    async def action_refresh(self) -> None:
        try:
            o = await self.app.client._request("GET", A, timeout=60.0)
        except ApiError as exc:
            self.query_one("#st-status", Label).update(f"Couldn't reach the file server API: {exc}")
            return
        self._o = o
        s = o["server"]
        self.query_one("#st-server", Label).update(
            f"File server: {'running · ' + s['urls']['web'] + ' · WebDAV ' + s['urls']['webdav'] if s.get('running') else 'stopped'}"
            + (f" · ALERTS on {', '.join(a['share'] for a in o['alerts'])}" if o["alerts"] else "")
            + (f" · frozen: {', '.join(o['frozen'])}" if o["frozen"] else ""))
        a = o["array"]
        self.query_one("#st-array", Label).update(
            (f"Array: {len(a['disks'])} disk(s), {'dual' if a['dual_parity'] else 'single'} parity, {a['pending_parity']} stripe(s) "
             f"pending; {'; '.join(a['warnings']) or 'no warnings'}") if a["configured"] else "Array: not set up (Storage page or `abp nas array set`)")
        d = self.query_one("#st-disks", DataTable)
        d.clear()
        for x in a["disks"]:
            d.add_row(x["name"], x["state"], str(x["files"]), f"{x['protected_bytes'] >> 20} MiB", x["path"])
        t = self.query_one("#st-shares", DataTable)
        t.clear()
        for x in o["shares"]:
            t.add_row(x["name"], x["access"], x.get("path") or f"cache {x['cache']}", x.get("comment", ""))

    async def _follow(self, started: dict) -> None:
        seen = 0
        while True:
            run = await self.app.client._request("GET", f"{A}/runs/{started['run']}", params={"since": seen})
            for line in run["log"]:
                self._log(line)
            seen = run["log_total"]
            if run["done"]:
                self._log(f"{started['title']}: {'failed: ' + run['error'] if run.get('error') else 'done'}")
                await self.action_refresh()
                return
            await asyncio.sleep(1.5)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        r = self.app.client._request
        b = event.button.id
        try:
            if b == "st-refresh":
                await self.action_refresh()
            elif b == "st-srv":
                await r("POST", f"{A}/server/{'stop' if self._o['server'].get('running') else 'start'}")
                await self.action_refresh()
            elif b == "st-sync":
                await self._follow(await r("POST", f"{A}/array/sync", json={}))
            elif b == "st-scrub":
                await self._follow(await r("POST", f"{A}/array/scrub", json={"percent": 10}))
            elif b == "st-mover":
                await self._follow(await r("POST", f"{A}/mover", json={}))
            elif b == "st-index":
                await self._follow(await r("POST", f"{A}/index", json={}))
            elif b == "st-health":
                inv = await r("GET", f"{A}/disks", timeout=180.0)
                for x in inv["drives"]:
                    self._log(f"{x['model'][:32]:<32} {x['risk']['band']:<12} {'; '.join(x['risk']['reasons'])}")
            elif b == "st-search":
                q = self.query_one("#st-q", Input).value.strip()
                if q:
                    for h in await r("GET", f"{A}/search", params={"q": q}, timeout=120.0):
                        self._log(f"{h['share']}/{h['path']}  {h['snippet'] or ', '.join(h['tags'])}")
        except ApiError as exc:
            self.query_one("#st-status", Label).update(f"error: {exc}")
