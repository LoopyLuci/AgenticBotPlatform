"""ABP Web Hosting in the terminal (the dashboard's /api/hosting, like the Hosting page and `abp host`): the sites and
how they are exposed, the web server, connected accounts, network situation; create a site, go live (with the run's
log as it happens), publish, check from outside, start/stop the web server, verify an account. Connecting an account
(typing its secrets) stays on the Hosting page or `abp host account add`, which prompts without echoing."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Log, Select, Static

from bot.tui.client import ApiError

API = "/api/hosting"
MODES = [("Cloudflare Tunnel", "cloudflare-tunnel"), ("Tailscale Funnel", "tailscale-funnel"), ("Port forward", "port-forward"),
         ("Direct (public IP)", "direct"), ("My server (SSH)", "server"), ("Hosting provider", "provider"), ("LAN only", "lan")]


class HostingScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Static("Hosting", id="host-title")
        yield Label("", id="host-server")
        yield Label("", id="host-network")
        yield DataTable(id="host-sites")
        with Horizontal():
            yield Select(MODES, prompt="Go-live mode", id="host-mode")
            yield Select([], prompt="Account (tunnel, server, provider)", id="host-account")
            yield Button("Go live", id="host-live", variant="primary")
            yield Button("Publish", id="host-publish")
            yield Button("Check", id="host-check")
            yield Button("Remove", id="host-remove")
        with Vertical(id="host-form"):
            with Horizontal():
                yield Input(placeholder="new site: name", id="host-name")
                yield Input(placeholder="folder (static) or http://127.0.0.1:PORT (app)", id="host-src")
                yield Input(placeholder="domains, comma separated", id="host-domains")
                yield Button("Create", id="host-create")
        with Horizontal():
            yield Button("Start/stop web server", id="host-edge")
            yield Button("Verify accounts", id="host-verify")
            yield Button("Refresh (r)", id="host-refresh")
        yield Log(id="host-log", max_lines=400)
        yield Label("", id="host-status")
        yield Footer()

    async def on_mount(self) -> None:
        t = self.query_one("#host-sites", DataTable)
        t.cursor_type = "row"
        t.add_columns("Site", "Kind", "Live via", "Domains", "Last")
        await self.action_refresh()

    def _say(self, text: str) -> None:
        self.query_one("#host-status", Label).update(text)

    def _log(self, text: str) -> None:
        self.query_one("#host-log", Log).write_line(text)

    async def action_refresh(self) -> None:
        r = self.app.client._request
        try:
            o = await r("GET", API)
        except ApiError as exc:
            self._say(f"Couldn't reach hosting: {exc}")
            return
        self._data = o
        e = o["edge"]
        self.query_one("#host-server", Label).update(
            f"Web server: {e['engine']} {'running' if e.get('running') else 'stopped'} · http {e['http_port']} · https {e['https_port']}"
            f" · accounts: {', '.join(a['name'] for a in o['accounts']) or 'none'}")
        t = self.query_one("#host-sites", DataTable)
        t.clear()
        for s in o["sites"]:
            last = (s.get("history") or [{}])[-1]
            t.add_row(s["id"], s["kind"], (s.get("exposure") or {}).get("mode", "-"), ", ".join(s.get("domains") or []),
                      f"{last.get('action', '')} {'ok' if last.get('ok') else ('failed' if last else '')}", key=s["id"])
        self.query_one("#host-account", Select).set_options([(f"{a['name']} ({a['label']})", a["id"]) for a in o["accounts"]])
        self._say(f"{len(o['sites'])} site(s)")
        self._network_task = asyncio.create_task(self._network())   # a reference: tasks can be collected mid-run

    async def _network(self) -> None:
        try:
            n = await self.app.client._request("GET", f"{API}/network", timeout=60.0)
            net = n["network"]
            self.query_one("#host-network", Label).update(
                f"Public {net['public_ipv4'] or '-'} · LAN {net['lan_ip'] or '-'} · {net['situation']} · recommended: {n['mode']}")
        except ApiError as exc:
            self.query_one("#host-network", Label).update(f"network: {exc}")

    def _selected(self) -> str | None:
        t = self.query_one("#host-sites", DataTable)
        if t.row_count == 0:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    async def _follow(self, started: dict) -> None:
        seen = 0
        while True:
            run = await self.app.client._request("GET", f"{API}/runs/{started['run']}", params={"since": seen})
            for line in run["log"]:
                self._log(line)
            seen = run["log_total"]
            if run["done"]:
                res = run.get("result")
                if run.get("error"):
                    self._say(f"failed: {run['error']}")
                elif isinstance(res, dict) and "steps" in res:
                    for s in res["steps"]:
                        self._log(f"{'✓' if s['ok'] else '✗'} {s['text']}: {s['note']}")
                    self._say("live" if res.get("ok") else "some steps need attention (see the log)")
                else:
                    self._say("done")
                await self.action_refresh()
                return
            await asyncio.sleep(1.5)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        r = self.app.client._request
        sid = self._selected()
        bid = event.button.id
        try:
            if bid == "host-refresh":
                await self.action_refresh()
            elif bid == "host-create":
                name = self.query_one("#host-name", Input).value.strip()
                src = self.query_one("#host-src", Input).value.strip()
                body = {"name": name or "Site", "domains": self.query_one("#host-domains", Input).value}
                if src.startswith("http"):
                    body.update(kind="proxy", upstream=src)
                else:
                    body.update(kind="static", root=src)
                s = await r("POST", f"{API}/sites", json=body)
                self._say(f"created {s['id']}")
                await self.action_refresh()
            elif bid == "host-edge":
                running = self._data["edge"].get("running")
                await r("POST", f"{API}/edge/{'stop' if running else 'start'}")
                await self.action_refresh()
            elif bid == "host-verify":
                for a in self._data["accounts"]:
                    v = await r("POST", f"{API}/accounts/{a['id']}/verify", timeout=60.0)
                    self._log(f"{a['name']}: {'works' if v['ok'] else 'NOT working'} — {v['note']}")
                await self.action_refresh()
            elif not sid:
                self._say("select a site first")
            elif bid == "host-live":
                mode = self.query_one("#host-mode", Select).value
                acc = self.query_one("#host-account", Select).value
                body = {"mode": mode if isinstance(mode, str) else "", "account": acc if isinstance(acc, str) else ""}
                self._log(f"— go live: {sid} ({body['mode'] or 'recommended'})")
                await self._follow(await r("POST", f"{API}/sites/{sid}/go-live", json=body))
            elif bid == "host-publish":
                self._log(f"— publish: {sid}")
                await self._follow(await r("POST", f"{API}/sites/{sid}/publish", json={}))
            elif bid == "host-check":
                c = await r("POST", f"{API}/sites/{sid}/check", timeout=120.0)
                for n in c["names"]:
                    self._log(f"{'✓' if n.get('ok') else '✗'} {n['name']}: {n.get('problem') or 'https ' + str(n['https'].get('status'))}")
                self._say("all names answer" if c["ok"] else "problems (see the log)")
            elif bid == "host-remove":
                await r("DELETE", f"{API}/sites/{sid}")
                await self.action_refresh()
        except ApiError as exc:
            self._say(f"error: {exc}")
