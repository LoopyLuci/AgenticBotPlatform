"""Local AI and the Neural Lab in the terminal (the dashboard's /api/localai and /api/lab, like the Local AI and Neural
Lab pages and `abp ai` / `abp lab`): the model server, models, fine-tuning and lab runs with their progress, the system
models and the CPU policy; start/stop the server, pull a model, measure the drives or the GPU."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Log, Static

from bot.tui.client import ApiError

A, L = "/api/localai", "/api/lab"
ACTIVE = ("starting", "loading", "training", "merging", "stopping")


class LocalAIScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Static("Local AI and the Neural Lab", id="ai-title")
        yield Label("", id="ai-server")
        yield Label("", id="ai-policy")
        yield DataTable(id="ai-models")
        yield DataTable(id="ai-runs")
        with Horizontal():
            yield Button("Start/stop server", id="ai-srv", variant="primary")
            yield Button("Discover", id="ai-disc")
            yield Button("Measure drives", id="ai-bench-d")
            yield Button("Measure GPU", id="ai-bench-g")
            yield Button("System models", id="ai-tune")
            yield Button("Refresh (r)", id="ai-refresh")
        with Horizontal():
            yield Input(placeholder="pull a model: qwen2.5:0.5b or hf.co/org/repo:Q4_K_M", id="ai-pull")
            yield Button("Pull", id="ai-pull-go")
        yield Log(id="ai-log", max_lines=500)
        yield Label("", id="ai-status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#ai-models", DataTable).add_columns("Model", "Size", "Quant", "Source")
        self.query_one("#ai-runs", DataTable).add_columns("Run", "Kind", "State", "Name", "Progress")
        self._o = {"server": {}}
        await self.action_refresh()
        self.set_interval(5, self._poll)

    def _log(self, text: str) -> None:
        self.query_one("#ai-log", Log).write_line(text)

    async def _poll(self) -> None:
        if any(r.get("state") in ACTIVE for r in getattr(self, "_runs", [])):
            await self.action_refresh()

    async def action_refresh(self) -> None:
        r = self.app.client._request
        try:
            o = await r("GET", A, timeout=60.0)
            lab = await r("GET", L, timeout=60.0)
        except ApiError as exc:
            self.query_one("#ai-status", Label).update(f"Couldn't reach the local AI API: {exc}")
            return
        self._o = o
        s, e = o["server"], o.get("engine")
        self.query_one("#ai-server", Label).update(
            f"Server: {'running · ' + s['url'] + ' (Ollama API)' if s.get('running') else 'stopped'} · engine "
            f"{e['build'] + ' ' + e['backend'] if e else 'not installed'} · "
            f"{', '.join(g['name'] for g in o['gpus']) or 'no GPU'} · {len(o['running'])} loaded")
        pol = lab["systune"]["cpu_policy"]
        self.query_one("#ai-policy", Label).update(
            f"CPU policy: {pol['level']}, {pol['threads']} thread(s)" + (f", avoiding CPU {pol['avoid']}" if pol["avoid"] else "")
            + (f" — {pol['reasons'][0]}" if pol["reasons"] else ""))
        t = self.query_one("#ai-models", DataTable)
        t.clear()
        for m in o["models"]:
            t.add_row(m["name"], f"{m['size'] / 2**30:.2f} GB", m["details"].get("quantization_level", ""), m.get("source", ""))
        runs = [("fine-tune", x) for x in o["runs"][:8]] + [("lab", x) for x in lab["runs"][:8]]
        self._runs = [x for _, x in runs]
        rt = self.query_one("#ai-runs", DataTable)
        rt.clear()
        for kind, x in runs:
            fin = x.get("final") or {}
            prog = (" ".join(f"{k[4:]}={v}" for k, v in fin.items() if k.startswith("val_")) if x.get("state") == "done"
                    else f"step {x.get('step', 0)}/{x.get('total_steps', '?')} loss {x.get('loss', '-')}")
            rt.add_row(x["id"], kind, x.get("state", ""), str(x.get("name", "")), prog)

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
            if b == "ai-refresh":
                await self.action_refresh()
            elif b == "ai-srv":
                await r("POST", f"{A}/server/{'stop' if self._o['server'].get('running') else 'start'}", timeout=60.0)
                await self.action_refresh()
            elif b == "ai-pull-go":
                name = self.query_one("#ai-pull", Input).value.strip()
                if name:
                    await self._follow(await r("POST", f"{A}/models/pull", json={"name": name}))
            elif b == "ai-disc":
                d = await r("GET", f"{A}/discover", timeout=180.0)
                for it in d["found"]:
                    self._log(f"{it['app']:<13} {it['kind']:<12} {it.get('name') or it['path']}{'  (in use)' if it.get('adopted') else ''}")
            elif b == "ai-bench-d":
                await self._follow(await r("POST", f"{L}/systune/bench", json={"kind": "transfer"}))
            elif b == "ai-bench-g":
                await self._follow(await r("POST", f"{L}/systune/bench", json={"kind": "llm"}))
            elif b == "ai-tune":
                st = await r("GET", f"{L}/systune", timeout=120.0)
                for k, v in st["models"].items():
                    self._log(f"{k:<10} {'in use' if v['adopted'] else 'learning'}  {v['measurements']}/{v['needed']}  {v.get('metrics') or ''}")
        except ApiError as exc:
            self.query_one("#ai-status", Label).update(f"error: {exc}")
