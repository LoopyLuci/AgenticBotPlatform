"""Terminal dashboard for the CI/CD platform (Textual).

    python -m abp_cicd tui [--source local|http] [--url URL] [--token T]

Every tab is one panel from `views.PANELS`, loaded through `views.load_panel`
— the same data the GUI shows. Keys: 1-7 switch tab, r refresh, q quit.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TabbedContent, TabPane

from . import views
from .transport import TransportError

PANEL_IDS = tuple(views.PANELS)
BAR_WIDTH = 40


def _bar(ms: int, total: int, width: int = BAR_WIDTH) -> str:
    return "█" * max(1 if ms else 0, int(width * ms / max(1, total)))


class DashboardApp(App):
    TITLE = "ABP CI-CD Dashboard"
    CSS = """
    #runs-table { width: 46%; }
    #runs-detail { width: 54%; padding: 0 1; }
    Static.body { padding: 1 2; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh"),
                *[(str(i + 1), f"tab('{pid}')", views.PANELS[pid][0]) for i, pid in enumerate(PANEL_IDS)]]

    def __init__(self, transport: Any, refresh_s: float = 2.0):
        super().__init__()
        self.transport = transport
        self.refresh_s = refresh_s
        self.selected_run: Optional[str] = None
        self.last_seq = 0
        self.events_shown = 0     # events delivered to the log (RichLog defers rendering while its tab is hidden)
        self.errors = 0

    # ---- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for pid in PANEL_IDS:
                with TabPane(views.PANELS[pid][0], id=pid):
                    if pid == "runs":
                        with Horizontal():
                            yield DataTable(id="runs-table", cursor_type="row")
                            yield Static("", id="runs-detail", markup=False)
                    elif pid in ("decisions", "workers"):
                        yield DataTable(id=f"{pid}-table")
                    elif pid == "events":
                        yield RichLog(id="events-log", wrap=False, highlight=False, markup=False, max_lines=2000)
                    else:
                        yield Static("loading…", id=f"{pid}-body", classes="body", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#runs-table", DataTable).add_columns("id", "kind", "status", "took", "what")
        self.query_one("#decisions-table", DataTable).add_columns("when", "run", "actor", "decision", "reason", "conf")
        self.query_one("#workers-table", DataTable).add_columns("worker", "state", "model", "queue", "detail", "seen")
        await self.refresh_all()
        self.set_interval(self.refresh_s, self.refresh_all)

    def action_tab(self, pid: str) -> None:
        self.query_one("#tabs", TabbedContent).active = pid

    async def action_refresh(self) -> None:
        await self.refresh_all()

    # ---- data -------------------------------------------------------------
    async def refresh_all(self) -> None:
        def fetch() -> dict:
            out: dict[str, dict] = {}
            for pid in PANEL_IDS:
                out[pid] = views.load_panel(self.transport, pid, selected_run=self.selected_run, since=self.last_seq)
            return out
        try:
            data = await asyncio.to_thread(fetch)
        except TransportError as exc:
            self.errors += 1
            self.sub_title = f"disconnected: {exc}"
            return
        except Exception as exc:  # noqa: BLE001 - a refresh must never crash the UI
            self.errors += 1
            self.sub_title = f"refresh failed: {type(exc).__name__}: {exc}"
            return
        self.sub_title = "connected"
        for pid, payload in data.items():
            getattr(self, f"_apply_{pid}")(payload)

    # ---- panels -----------------------------------------------------------
    def _apply_overview(self, d: dict) -> None:
        lines = []
        for t in d["tiles"]:
            lines.append(f"{t['title']:<14} {t['value']:<12} {t['detail']}")
        s = d["summary"]
        if s["active"]:
            lines += ["", "running now: " + ", ".join(f"{r['kind']} {r['id']}" for r in s["active"])]
        self.query_one("#overview-body", Static).update("\n".join(lines))

    def _apply_runs(self, d: dict) -> None:
        table = self.query_one("#runs-table", DataTable)
        keep = self.selected_run
        table.clear()
        for r in d["runs"]:
            table.add_row(r["id"], r["kind"] or "-", r["status"], views.fmt_ms(r["duration_ms"]),
                          r["attrs"].get("version") or r["attrs"].get("title") or "", key=r["id"])
        if keep and keep in [r["id"] for r in d["runs"]]:
            table.move_cursor(row=[r["id"] for r in d["runs"]].index(keep))
        self._show_run(d)

    def _show_run(self, d: dict) -> None:
        detail = self.query_one("#runs-detail", Static)
        run = d.get("run")
        if not run:
            detail.update("no runs recorded yet")
            return
        g = d["gantt"]
        lines = [f"{run['kind']} {run['id']}  {run['status']}  {views.fmt_ms(run['duration_ms'])}", ""]
        for b in g["bars"]:
            mark = "  <- slowest" if b["slowest"] else ""
            lines.append(f"{b['name']:<16} {b['status']:<8} {views.fmt_ms(b['duration_ms']):>9}  "
                         f"{' ' * int(BAR_WIDTH * b['offset_ms'] / max(1, g['total_ms']))}"
                         f"{_bar(b['duration_ms'], g['total_ms'])}{mark}")
        lines += ["", d["explain"]]
        detail.update("\n".join(lines))

    def _apply_steps(self, d: dict) -> None:
        rows = d["bars"]
        if not rows:
            self.query_one("#steps-body", Static).update("no step timings recorded yet")
            return
        top = max(r["p95_ms"] or r["p50_ms"] for r in rows) or 1
        lines = [f"{'step':<16} {'p50':>9} {'p95':>9} {'runs':>5} {'fail':>5}"]
        for r in rows:
            lines.append(f"{r['name']:<16} {views.fmt_ms(r['p50_ms']):>9} {views.fmt_ms(r['p95_ms']):>9} "
                         f"{r['n']:>5} {r['failed']:>5}  {_bar(r['p50_ms'], top)}")
        self.query_one("#steps-body", Static).update("\n".join(lines))

    def _apply_decisions(self, d: dict) -> None:
        table = self.query_one("#decisions-table", DataTable)
        table.clear()
        for x in d["decisions"]:
            table.add_row(views.fmt_time(x["ts"]), x.get("run_id") or "", x.get("actor") or "", x.get("decision") or "",
                          (x.get("reason") or "")[:60], "" if x.get("confidence") is None else f"{x['confidence']:.2f}")

    def _apply_workers(self, d: dict) -> None:
        table = self.query_one("#workers-table", DataTable)
        table.clear()
        for w in d["workers"]:
            table.add_row(w["worker"], w["state"], w["model"] or "-", "-" if w["queue"] is None else str(w["queue"]),
                          (w["detail"] or "")[:40], f"{w['age_s']}s ago")

    def _apply_events(self, d: dict) -> None:
        log = self.query_one("#events-log", RichLog)
        for e in d["events"]:
            log.write(views.event_line(e))
        self.events_shown += len(d["events"])
        self.last_seq = d["last_seq"]

    def _apply_integrity(self, d: dict) -> None:
        c = d["chain"]
        text = (f"Event log verified: {c['count']} events, hash chain intact."
                if c["ok"] else f"CHAIN BROKEN at event {c['first_bad_seq']}: {c['reason']}")
        self.query_one("#integrity-body", Static).update(text)

    # ---- selection --------------------------------------------------------
    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "runs-table" or event.row_key is None or event.row_key.value is None:
            return
        rid = str(event.row_key.value)
        if rid != self.selected_run:
            self.selected_run = rid
            d = await asyncio.to_thread(views.load_panel, self.transport, "runs", selected_run=rid)
            self._show_run(d)


def run(transport: Any, refresh_s: float = 2.0) -> None:
    DashboardApp(transport, refresh_s).run()
