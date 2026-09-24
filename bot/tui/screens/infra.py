"""Terminal screens for Tailscale, Containers, Virtual Machines and Infra Automation - the same features the
dashboard pages have, over the same /api/tailscale, /api/docker, /api/vms and /api/infra/rules routes (through
DashboardClient.infra), including the "Manage" host picker for linked servers.

Interactive shells: on the local host, "Shell" hands the real terminal to `docker exec -it` (Textual suspends
itself, exactly like running it by hand). For a linked server, or for a VM console, use the desktop app or the
dashboard, which relay a PTY over WebSocket; here the VM screen instead has a QEMU monitor prompt."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from textual.app import ComposeResult, SuspendNotSupported
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, Static

from bot.tui.client import ApiError


def _cell(v: Any) -> str:
    return "" if v is None else str(v)


class InfraScreen(Screen):
    """Shared shape: title, host picker, view picker, a table, an output log and a row of action buttons."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]
    TITLE_TEXT = ""
    AREA = "docker"
    VIEWS: tuple[tuple[str, str], ...] = ()
    ID = "infra"

    def __init__(self) -> None:
        super().__init__()
        self.host = "local"
        self.view = self.VIEWS[0][1] if self.VIEWS else ""
        self._generation = 0

    # ------------------------------------------------------------ layout
    def buttons(self) -> list[tuple[str, str]]:
        return []

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE_TEXT, id=f"{self.ID}-title")
        with Horizontal():
            yield Select([("This machine", "local")], value="local", allow_blank=False, id=f"{self.ID}-host")
            if self.VIEWS:
                yield Select(list(self.VIEWS), value=self.view, allow_blank=False, id=f"{self.ID}-view")
        yield DataTable(id=f"{self.ID}-table")
        with Horizontal():
            for label, bid in self.buttons():
                yield Button(label, id=f"{self.ID}-{bid}")
            yield Button("Refresh", id=f"{self.ID}-refresh")
        yield from self.extra_widgets()
        yield RichLog(id=f"{self.ID}-out", wrap=True, highlight=False, markup=False, max_lines=2000)
        yield Label("", id=f"{self.ID}-status")
        yield Footer()

    def extra_widgets(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    async def on_mount(self) -> None:
        table = self.query_one(f"#{self.ID}-table", DataTable)
        table.cursor_type = "row"
        try:
            hosts = await self.app.client.infra_hosts()
            self.query_one(f"#{self.ID}-host", Select).set_options(
                [(h["name"] + ("" if h["local"] else " (linked)"), h["id"]) for h in hosts])
            self.query_one(f"#{self.ID}-host", Select).value = "local"
        except ApiError:
            pass
        await self.action_refresh()

    # ------------------------------------------------------------ helpers
    def say(self, text: str, error: bool = False) -> None:
        self.query_one(f"#{self.ID}-status", Label).update(("Error: " if error else "") + text)

    def out(self, text: Any) -> None:
        log = self.query_one(f"#{self.ID}-out", RichLog)
        log.write(text if isinstance(text, str) else json.dumps(text, indent=2, default=str))

    async def call(self, method: str, path: str, body: Any = None, *, area: Optional[str] = None) -> Any:
        return await self.app.client.infra(area or self.AREA, method, path, body, host=self.host)

    def selected(self) -> Optional[str]:
        table = self.query_one(f"#{self.ID}-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def fill(self, columns: tuple[str, ...], rows: list[tuple[str, tuple]]) -> None:
        table = self.query_one(f"#{self.ID}-table", DataTable)
        table.clear(columns=True)
        table.add_columns(*columns)
        for key, cells in rows:
            table.add_row(*[_cell(c) for c in cells], key=key)

    # ------------------------------------------------------------ events
    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == f"{self.ID}-host" and event.value != self.host and event.value is not Select.BLANK:
            self.host = str(event.value)
            await self.action_refresh()
        elif event.select.id == f"{self.ID}-view" and event.value != self.view and event.value is not Select.BLANK:
            self.view = str(event.value)
            await self.action_refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = (event.button.id or "")[len(self.ID) + 1:]
        if bid == "refresh":
            await self.action_refresh()
            return
        try:
            await self.on_action(bid)
        except ApiError as exc:
            self.say(exc.detail, error=True)

    async def action_refresh(self) -> None:
        self._generation += 1
        gen = self._generation
        self.say("")
        try:
            await self.load()
        except ApiError as exc:
            if gen == self._generation:
                self.fill(("Error",), [("err", (exc.detail,))])
                self.say(exc.detail, error=True)

    async def load(self) -> None:
        raise NotImplementedError

    async def on_action(self, bid: str) -> None:
        raise NotImplementedError


# ==================================================================== Containers
class ContainersScreen(InfraScreen):
    TITLE_TEXT = "Containers (Docker)"
    ID = "ct"
    VIEWS = (("Containers", "containers"), ("Images", "images"), ("Volumes", "volumes"), ("Networks", "networks"),
             ("Stacks", "stacks"), ("App templates", "templates"), ("System", "df"))

    def buttons(self) -> list[tuple[str, str]]:
        return [("Start", "start"), ("Stop", "stop"), ("Restart", "restart"), ("Logs", "logs"), ("Shell", "shell"),
                ("Remove", "remove"), ("Deploy template", "template"), ("Prune unused", "prune")]

    def extra_widgets(self) -> ComposeResult:
        with Horizontal():
            yield Input(placeholder="image to deploy (e.g. nginx:stable)", id="ct-image")
            yield Input(placeholder="name", id="ct-name")
            yield Input(placeholder="ports (8080:80,...)", id="ct-ports")
            yield Button("Deploy container", id="ct-deploy", variant="primary")
            yield Input(placeholder="command to run in selected", id="ct-exec-cmd")
            yield Button("Run", id="ct-exec")

    async def load(self) -> None:
        info = await self.call("GET", "info")
        if not info.get("running"):
            self.fill(("Docker",), [("down", (f"Docker is not responding: {info.get('error', 'not installed')}",))])
            self.say("Start Docker Desktop (or the Docker service), then press r.", error=True)
            return
        data = await self.call("GET", self.view)
        v = self.view
        if v == "containers":
            self.fill(("Name", "Image", "State", "Status", "Ports"),
                      [(c["Names"], (c["Names"], c["Image"], c["State"], c["Status"], c.get("Ports"))) for c in data])
        elif v == "images":
            self.fill(("Repository", "Tag", "ID", "Size"),
                      [(i["ID"], (i["Repository"], i["Tag"], i["ID"][7:19], i["Size"])) for i in data])
        elif v == "volumes":
            self.fill(("Name", "Driver"), [(x["Name"], (x["Name"], x["Driver"])) for x in data])
        elif v == "networks":
            self.fill(("Name", "Driver", "Scope"), [(x["Name"], (x["Name"], x["Driver"], x["Scope"])) for x in data])
        elif v == "stacks":
            self.fill(("Stack", "Status"), [(x["Name"], (x["Name"], x["Status"])) for x in data])
        elif v == "templates":
            self.fill(("App", "Image", "Ports"), [(t["id"], (t["title"], t["image"], ",".join(t.get("ports", [])))) for t in data])
        else:
            self.fill(("Type", "Total", "Active", "Size", "Reclaimable"),
                      [(x["Type"], (x["Type"], x["TotalCount"], x["Active"], x["Size"], x["Reclaimable"])) for x in data])

    async def on_action(self, bid: str) -> None:
        sel = self.selected()
        v = self.view
        if bid == "deploy":
            image = self.query_one("#ct-image", Input).value.strip()
            ports = [p.strip() for p in self.query_one("#ct-ports", Input).value.split(",") if p.strip()]
            name = self.query_one("#ct-name", Input).value.strip() or None
            r = await self.call("POST", "containers", {"image": image, "name": name, "ports": ports, "restart": "unless-stopped"})
            self.say(f"Started {r.get('id', '')[:12]}")
            await self.action_refresh()
            return
        if bid == "prune":
            kind = {"containers": "container", "images": "image", "volumes": "volume", "networks": "network"}.get(v, "image")
            self.out(await self.call("POST", "prune", {"kind": kind}))
            await self.action_refresh()
            return
        if sel is None:
            self.say("Select a row first.", error=True)
            return
        if bid == "template" and v == "templates":
            await self.call("POST", f"templates/{sel}/deploy", {})
            self.say(f"Deployed {sel}")
        elif v == "containers" and bid in ("start", "stop", "restart", "remove"):
            await self.call("POST", f"containers/{sel}/action", {"action": bid})
            self.say(f"{bid}: {sel}")
            await self.action_refresh()
        elif v == "containers" and bid == "logs":
            self.out((await self.call("GET", f"containers/{sel}/logs?tail=200"))["logs"])
        elif v == "containers" and bid == "exec":
            cmd = self.query_one("#ct-exec-cmd", Input).value.split()
            self.out((await self.call("POST", f"containers/{sel}/exec", {"command": cmd}))["output"])
        elif v == "containers" and bid == "shell":
            await self.shell_into(sel)
        elif bid == "remove":
            path = {"images": "images/remove", "volumes": None, "networks": None}.get(v)
            if v == "images":
                await self.call("POST", path, {"ref": sel})
            elif v == "volumes":
                await self.call("DELETE", f"volumes/{sel}")
            elif v == "networks":
                await self.call("DELETE", f"networks/{sel}")
            else:
                self.say("Nothing to remove in this view.", error=True)
                return
            self.say(f"Removed {sel}")
            await self.action_refresh()
        elif v == "stacks" and bid in ("start", "stop", "restart"):
            self.out(await self.call("POST", f"stacks/{sel}/action", {"action": bid}))
        else:
            self.say("That action does not apply to this view.", error=True)

    async def shell_into(self, container: str) -> None:
        if self.host != "local":
            self.say("Shells on a linked server are available in the desktop app and the dashboard.", error=True)
            return
        from bot import terminal_broker as tb

        try:
            argv = tb.container_argv(container)
        except Exception as exc:  # TerminalError / DockerError
            self.say(str(exc), error=True)
            return
        try:
            with self.app.suspend():
                subprocess.run(argv)
        except SuspendNotSupported:
            self.say("This terminal can't hand over the screen; run the shell from the desktop app or the dashboard.", error=True)


# ========================================================================== VMs
class VmsScreen(InfraScreen):
    TITLE_TEXT = "Virtual machines (QEMU / Hyper-V / libvirt)"
    AREA = "vm"
    ID = "vm"

    def buttons(self) -> list[tuple[str, str]]:
        return [("Start", "start"), ("Shut down", "stop"), ("Force off", "force"), ("Pause", "pause"),
                ("Resume", "resume"), ("Reset", "reset"), ("Snapshot", "snap"), ("Details", "detail"), ("Delete", "delete")]

    def extra_widgets(self) -> ComposeResult:
        with Horizontal():
            yield Input(placeholder="snapshot name", id="vm-tag")
            yield Input(placeholder="QEMU monitor command (e.g. info status)", id="vm-mon")
            yield Button("Run monitor command", id="vm-monitor")

    async def load(self) -> None:
        data = await self.call("GET", "")
        rows: list[tuple[str, tuple]] = []
        for v in data.get("qemu") or []:
            if isinstance(v, dict) and "name" in v:
                rows.append((f"qemu:{v['name']}", ("qemu", v["name"], v.get("state"), v.get("cpus"), v.get("memory"))))
        for v in data.get("hyperv") if isinstance(data.get("hyperv"), list) else []:
            rows.append((f"hyperv:{v['Name']}", ("hyperv", v["Name"], {2: "Running", 3: "Off", 6: "Saved", 9: "Paused"}.get(v.get("State"), v.get("State")), v.get("ProcessorCount"), "")))
        for v in data.get("libvirt") if isinstance(data.get("libvirt"), list) else []:
            rows.append((f"libvirt:{v['name']}", ("libvirt", v["name"], v.get("state"), "", "")))
        self.fill(("Backend", "Name", "State", "CPUs", "Memory"), rows)

    async def on_action(self, bid: str) -> None:
        sel = self.selected()
        if sel is None:
            self.say("Select a machine first.", error=True)
            return
        if ":" not in sel:
            self.say("Select a machine first.", error=True)
            return
        backend, name = sel.split(":", 1)
        if bid == "monitor":
            if backend != "qemu":
                self.say("The monitor prompt is for QEMU machines.", error=True)
                return
            line = self.query_one("#vm-mon", Input).value.strip()
            r = await self.call("POST", f"qemu/{name}/monitor", {"command": line})
            self.out(f"(qemu) {line}\n{r.get('output', '')}")
            return
        if bid == "detail" and backend == "qemu":
            self.out(await self.call("GET", f"qemu/{name}"))
            return
        if bid == "snap" and backend == "qemu":
            r = await self.call("POST", f"qemu/{name}/snapshot", {"action": "create", "tag": self.query_one("#vm-tag", Input).value.strip()})
            self.out(r)
            return
        if bid == "delete" and backend == "qemu":
            await self.call("DELETE", f"qemu/{name}")
        elif backend == "qemu":
            body = {"force": True} if bid == "force" else {}
            await self.call("POST", f"qemu/{name}/{'stop' if bid == 'force' else bid}", body)
        elif backend == "hyperv":
            op = {"stop": "stop", "force": "force-stop", "delete": "delete"}.get(bid, bid)
            await self.call("POST", f"hyperv/{name}/{op}", {})
        elif backend == "libvirt":
            op = {"stop": "stop", "force": "force-stop"}.get(bid, bid)
            await self.call("POST", f"libvirt/{name}/{op}", {})
        self.say(f"{bid}: {name}")
        await self.action_refresh()


# ==================================================================== Tailscale
class TailscaleScreen(InfraScreen):
    TITLE_TEXT = "Tailscale"
    AREA = "tailscale"
    ID = "ts"
    VIEWS = (("Peers", "peers"), ("Serve & Funnel", "serve"), ("Preferences", "prefs"), ("Devices (needs API key)", "devices"))
    BOOLS = ("accept_dns", "accept_routes", "advertise_exit_node", "shields_up", "ssh", "auto_update", "update_check",
             "report_posture", "webclient")

    def buttons(self) -> list[tuple[str, str]]:
        return [("Connect", "up"), ("Disconnect", "down"), ("Ping", "ping"), ("Use as exit node", "exit"),
                ("Stop using exit node", "exit_off"), ("Stop publishing", "unserve"), ("Toggle preference", "toggle")]

    def extra_widgets(self) -> ComposeResult:
        with Horizontal():
            yield Input(placeholder="publish: port or URL (e.g. 3000)", id="ts-target")
            yield Select([("Tailnet only (Serve)", "serve"), ("Public internet (Funnel)", "funnel")], value="serve",
                         allow_blank=False, id="ts-how")
            yield Button("Publish", id="ts-publish", variant="primary")

    async def load(self) -> None:
        v = self.view
        if v == "peers":
            st = await self.call("GET", "status")
            self.fill(("Name", "IPs", "OS", "Online", "Exit node"),
                      [((p.get("TailscaleIPs") or [""])[0], (p.get("HostName"), ", ".join(p.get("TailscaleIPs") or []), p.get("OS"),
                                                             "yes" if p.get("Online") else "no", "in use" if p.get("ExitNode") else ("offered" if p.get("ExitNodeOption") else "")))
                       for p in (st.get("Peer") or {}).values()])
        elif v == "serve":
            s = await self.call("GET", "serve")
            funnel = s.get("AllowFunnel") or {}
            rows = []
            for host, w in (s.get("Web") or {}).items():
                for path, h in (w.get("Handlers") or {}).items():
                    rows.append((f"{host}|{path}|{1 if funnel.get(host) else 0}",
                                 (host, path, h.get("Proxy") or h.get("Path") or h.get("Text"), "PUBLIC" if funnel.get(host) else "tailnet")))
            self.fill(("Address", "Path", "Target", "Reach"), rows)
        elif v == "prefs":
            p = await self.call("GET", "prefs")
            cur = {"accept_dns": p.get("CorpDNS"), "accept_routes": p.get("RouteAll"), "shields_up": p.get("ShieldsUp"),
                   "ssh": p.get("RunSSH"), "auto_update": (p.get("AutoUpdate") or {}).get("Apply"),
                   "update_check": (p.get("AutoUpdate") or {}).get("Check"), "report_posture": p.get("PostureChecking"),
                   "webclient": p.get("RunWebClient"),
                   "advertise_exit_node": any(r == "0.0.0.0/0" for r in (p.get("AdvertiseRoutes") or []))}
            self.fill(("Setting", "On"), [(k, (k, "yes" if cur.get(k) else "no")) for k in self.BOOLS])
        else:
            d = await self.call("GET", "api/devices")
            self.fill(("Name", "IPs", "OS", "Authorized"),
                      [(x["id"], (x.get("name"), ", ".join(x.get("addresses") or []), x.get("os"), x.get("authorized"))) for x in d.get("devices", [])])

    async def on_action(self, bid: str) -> None:
        sel = self.selected()
        if bid in ("up", "down"):
            self.out(await self.call("POST", bid, {}))
            await self.action_refresh()
        elif bid == "publish":
            target = self.query_one("#ts-target", Input).value.strip()
            funnel = self.query_one("#ts-how", Select).value == "funnel"
            self.out(await self.call("POST", "serve", {"target": target, "funnel": funnel}))
            self.say("Published on the PUBLIC internet" if funnel else "Published to your tailnet")
        elif bid == "exit_off":
            await self.call("POST", "prefs", {"exit_node": ""})
            self.say("Exit node cleared")
        elif sel is None:
            self.say("Select a row first.", error=True)
        elif bid == "ping":
            self.out(await self.call("GET", f"ping?target={sel}"))
        elif bid == "exit":
            await self.call("POST", "prefs", {"exit_node": sel})
            self.say(f"Using {sel} as exit node")
        elif bid == "unserve":
            host, path, funnel = sel.split("|")
            port = int(host.rsplit(":", 1)[-1]) if ":" in host else 443
            await self.call("POST", "serve/off", {"funnel": funnel == "1", "port": port, "path": None if path == "/" else path})
            self.say("Stopped")
            await self.action_refresh()
        elif bid == "toggle" and self.view == "prefs":
            table = self.query_one("#ts-table", DataTable)
            current = table.get_row(sel)[1] == "yes"
            await self.call("POST", "prefs", {sel: not current})
            self.say(f"{sel} -> {'off' if current else 'on'}")
            await self.action_refresh()
        else:
            self.say("That action does not apply to this view.", error=True)


# ================================================================== Automation
class RulesScreen(InfraScreen):
    TITLE_TEXT = "Infra automation rules"
    AREA = "rules"
    ID = "ir"

    PRESETS = (
        ("Restart a container when it stops", "restart-container"),
        ("Restart a container when it is unhealthy", "restart-unhealthy"),
        ("Start a VM when it is off", "start-vm"),
        ("Reconnect Tailscale when it drops", "tailscale-up"),
        ("Prune unused images every 24h", "prune-daily"),
    )

    def buttons(self) -> list[tuple[str, str]]:
        return [("Enable/disable", "toggle"), ("Run now", "run"), ("History", "history"), ("Delete", "delete")]

    def extra_widgets(self) -> ComposeResult:
        with Horizontal():
            yield Select(list(self.PRESETS), value="restart-container", allow_blank=False, id="ir-preset")
            yield Input(placeholder="container / VM name", id="ir-target")
            yield Button("Create rule", id="ir-create", variant="primary")

    async def load(self) -> None:
        rules = await self.call("GET", "")
        self.fill(("Name", "On", "Trigger", "Action", "Runs", "Last result"),
                  [(str(r["id"]), (r["name"], "yes" if r["enabled"] else "no", json.dumps(r["trigger"]), json.dumps(r["action"]),
                                   r["runs"], (r.get("last_result") or "")[:60])) for r in rules])

    def preset_rule(self, preset: str, target: str) -> dict:
        t = target or "name"
        table = {
            "restart-container": ({"type": "watch", "resource": "container", "target": t, "when": "stopped"},
                                  {"type": "container", "target": t, "action": "restart"}),
            "restart-unhealthy": ({"type": "watch", "resource": "container", "target": t, "when": "unhealthy"},
                                  {"type": "container", "target": t, "action": "restart"}),
            "start-vm": ({"type": "watch", "resource": "vm", "target": t, "when": "off"},
                         {"type": "vm", "backend": "qemu", "target": t, "action": "start"}),
            "tailscale-up": ({"type": "watch", "resource": "tailscale", "when": "disconnected"}, {"type": "tailscale_up"}),
            "prune-daily": ({"type": "interval", "every": "24h"}, {"type": "prune", "kind": "image"}),
        }
        trigger, action = table[preset]
        return {"name": f"{preset} {t}" if preset not in ("tailscale-up", "prune-daily") else preset,
                "trigger": trigger, "action": action}

    async def on_action(self, bid: str) -> None:
        if bid == "create":
            body = self.preset_rule(str(self.query_one("#ir-preset", Select).value), self.query_one("#ir-target", Input).value.strip())
            await self.call("POST", "", body)
            self.say(f"Created: {body['name']}")
            await self.action_refresh()
            return
        sel = self.selected()
        if sel is None:
            self.say("Select a rule first.", error=True)
            return
        if bid == "toggle":
            table = self.query_one("#ir-table", DataTable)
            on = table.get_row(sel)[1] == "yes"
            await self.call("POST", f"{sel}/enable", {"enabled": not on})
        elif bid == "run":
            self.out(await self.call("POST", f"{sel}/run", {}))
        elif bid == "history":
            hist = await self.call("GET", f"{sel}/history")
            self.out("\n".join(f"{h['at']:.0f}  {'ok' if h['ok'] else 'FAILED'}  {h['detail']}" for h in hist) or "No runs yet")
        elif bid == "delete":
            await self.call("DELETE", sel)
        await self.action_refresh()
