"""ABP CI-CD Dashboard — the native desktop client (Tkinter, standard library).

    python -m abp_cicd.gui [--source local|http] [--db PATH] [--url URL] [--token T]
    (on Windows, double-click scripts/ABP_CI-CD_GUI.pyw)

One tab per panel in `views.PANELS`, loaded through `views.load_panel` — exactly
the data the terminal UI and CLI show. It only reads through a transport, so it
works against a local event store or a remote server. Fetching runs on a
background thread; the window never blocks on the network.
"""
from __future__ import annotations

import argparse
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from . import charts, views
from .transport import TransportError, choose

PANEL_IDS = tuple(views.PANELS)
TITLE = "ABP CI-CD Dashboard"
TILE_COLOURS = {"green": "#d6f0e0", "red": "#f8d7d3", "grey": "#eceff1", "blue": "#d7e6f8", "orange": "#fbe6c8"}
ROW_COLOURS = {"failed": "#b3261e", "aborted": "#b26a00", "rolled_back": "#b26a00", "skipped": "#6b7280",
               "running": "#1d4ed8", "stale": "#b26a00"}


class TkPainter:
    """charts.Painter backed by a Tk Canvas."""

    def __init__(self, canvas: tk.Canvas):
        self.c = canvas

    def rect(self, x, y, w, h, fill, outline=""):
        self.c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=outline or fill)

    def text(self, x, y, s, fill="#202124", anchor="w", bold=False):
        self.c.create_text(x, y, text=s, fill=fill, anchor=anchor, font=("Segoe UI", 9, "bold" if bold else "normal"))


def _tree(parent: tk.Misc, columns: list[tuple[str, int]]) -> ttk.Treeview:
    tree = ttk.Treeview(parent, columns=[c for c, _ in columns], show="headings", selectmode="browse")
    for name, width in columns:
        tree.heading(name, text=name)
        tree.column(name, width=width, anchor="w", stretch=True)
    for tag, colour in ROW_COLOURS.items():
        tree.tag_configure(tag, foreground=colour)
    sb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    return tree


class Dashboard:
    def __init__(self, root: tk.Tk, transport: Any, refresh_ms: int = 2000):
        self.root, self.transport, self.refresh_ms = root, transport, refresh_ms
        self.selected_run: Optional[str] = None
        self.last_seq = 0
        self._results: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._busy = False
        self._closed = False
        self._programmatic = False
        self.gantt_data: dict = {"total_ms": 0, "bars": []}
        root.title(TITLE)
        root.geometry("1100x680")
        root.minsize(760, 460)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)

    # ---- layout -----------------------------------------------------------
    def _build(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Refresh   (F5)", command=self.refresh)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.close)
        menu.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menu)
        self.root.bind("<F5>", lambda _e: self.refresh())

        self.status = tk.StringVar(value="connecting…")
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(8, 2)).pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True)
        self.frames: dict[str, ttk.Frame] = {}
        for pid in PANEL_IDS:
            frame = ttk.Frame(self.tabs, padding=6)
            self.frames[pid] = frame
            self.tabs.add(frame, text=views.PANELS[pid][0])
            getattr(self, f"_build_{pid}")(frame)

    def _build_overview(self, f: ttk.Frame) -> None:
        self.tiles = ttk.Frame(f)
        self.tiles.pack(fill="x")
        self.active_label = ttk.Label(f, text="", padding=(0, 12))
        self.active_label.pack(anchor="w")

    def _build_runs(self, f: ttk.Frame) -> None:
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        self.runs_tree = _tree(left, [("when", 110), ("kind", 70), ("status", 80), ("took", 70), ("what", 80)])
        self.runs_tree.bind("<<TreeviewSelect>>", self._on_run_selected)
        right = ttk.Frame(paned)
        self.gantt_canvas = tk.Canvas(right, height=220, background="white", highlightthickness=1,
                                      highlightbackground="#c9ccd1")
        self.gantt_canvas.pack(fill="x")
        self.gantt_canvas.bind("<Configure>", lambda _e: self._draw_gantt())
        self.explain_text = tk.Text(right, wrap="word", height=10, state="disabled", relief="flat", background="#f7f7f8")
        self.explain_text.pack(fill="both", expand=True, pady=(6, 0))
        paned.add(left, weight=2)
        paned.add(right, weight=3)
        self.root.after(60, lambda: paned.sashpos(0, 440))   # wide enough for every column

    def _build_steps(self, f: ttk.Frame) -> None:
        self.timing_canvas = tk.Canvas(f, background="white", highlightthickness=1, highlightbackground="#c9ccd1")
        self.timing_canvas.pack(fill="both", expand=True)
        self.timing_rows: list[dict] = []
        self.timing_canvas.bind("<Configure>", lambda _e: self._draw_timing())

    def _build_decisions(self, f: ttk.Frame) -> None:
        self.decisions_tree = _tree(f, [("when", 140), ("run", 150), ("actor", 90), ("decision", 200), ("reason", 300), ("conf", 50)])

    def _build_workers(self, f: ttk.Frame) -> None:
        self.workers_tree = _tree(f, [("worker", 160), ("state", 90), ("model", 120), ("queue", 60), ("detail", 300), ("seen", 90)])

    def _build_events(self, f: ttk.Frame) -> None:
        self.events_text = tk.Text(f, wrap="none", state="disabled", font=("Consolas", 9), background="#101418",
                                   foreground="#d6dbe0")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.events_text.yview)
        self.events_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.events_text.pack(fill="both", expand=True)

    def _build_integrity(self, f: ttk.Frame) -> None:
        self.integrity_label = ttk.Label(f, text="", font=("Segoe UI", 11), wraplength=800, justify="left")
        self.integrity_label.pack(anchor="w", pady=8)
        ttk.Button(f, text="Verify now", command=self.refresh).pack(anchor="w")

    # ---- data flow --------------------------------------------------------
    def _fetch_all(self) -> dict[str, dict]:
        return {pid: views.load_panel(self.transport, pid, selected_run=self.selected_run, since=self.last_seq)
                for pid in PANEL_IDS}

    def refresh(self) -> None:
        """Fetch on a worker thread; results are applied on the Tk thread."""
        if self._busy or self._closed:
            return
        self._busy = True

        def work() -> None:
            try:
                self._results.put(("ok", self._fetch_all()))
            except TransportError as exc:
                self._results.put(("offline", str(exc)))
            except Exception as exc:  # noqa: BLE001 - never let a refresh kill the window
                self._results.put(("error", f"{type(exc).__name__}: {exc}"))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(80, self._drain)

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            self.root.after(80, self._drain)
            return
        self._busy = False
        self.apply(kind, payload)
        self.root.after(self.refresh_ms, self.refresh)

    def apply(self, kind: str, payload: Any) -> None:
        if kind == "ok":
            for pid, data in payload.items():
                getattr(self, f"_apply_{pid}")(data)
            self.status.set(f"connected — updated {time.strftime('%H:%M:%S')}")
        elif kind == "offline":
            self.status.set(f"disconnected: {payload}")
        else:
            self.status.set(f"refresh failed: {payload}")

    def close(self) -> None:
        self._closed = True
        self.root.destroy()

    # ---- panels -----------------------------------------------------------
    def _apply_overview(self, d: dict) -> None:
        for child in self.tiles.winfo_children():
            child.destroy()
        for i, t in enumerate(d["tiles"]):
            box = tk.Frame(self.tiles, background=TILE_COLOURS[views.STATUS_STYLE.get(t["status"], "grey")], padx=14, pady=10)
            box.grid(row=0, column=i, padx=6, pady=4, sticky="nsew")
            self.tiles.columnconfigure(i, weight=1)
            tk.Label(box, text=t["title"], background=box["background"], font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(box, text=t["value"], background=box["background"], font=("Segoe UI", 15, "bold")).pack(anchor="w")
            tk.Label(box, text=t["detail"], background=box["background"], font=("Segoe UI", 8)).pack(anchor="w")
        active = d["summary"]["active"]
        self.active_label.config(text=("running now: " + ", ".join(f"{r['kind']} {r['id']}" for r in active)) if active else "")

    def _apply_runs(self, d: dict) -> None:
        tree = self.runs_tree
        tree.delete(*tree.get_children())
        for r in d["runs"]:
            when = time.strftime("%m-%d %H:%M", time.localtime(r["started"])) if r["started"] else "-"
            tree.insert("", "end", iid=r["id"], tags=(r["status"],),
                        values=(when, r["kind"] or "-", r["status"], views.fmt_ms(r["duration_ms"]),
                                r["attrs"].get("version") or r["attrs"].get("title") or ""))
        # Highlight what the detail pane is showing (the newest run unless the user picked another).
        if d["selected"] and tree.exists(d["selected"]):
            self._programmatic = True
            tree.selection_set(d["selected"])
            self.root.after_idle(self._clear_programmatic)
        self._show_run(d)

    def _clear_programmatic(self) -> None:
        self._programmatic = False

    def _show_run(self, d: dict) -> None:
        self.gantt_data = d["gantt"]
        self._draw_gantt()
        text = d["explain"] or "no runs recorded yet"
        self.explain_text.config(state="normal")
        self.explain_text.delete("1.0", "end")
        self.explain_text.insert("1.0", text)
        self.explain_text.config(state="disabled")

    def _on_run_selected(self, _event: Any) -> None:
        if self._programmatic:
            return
        sel = self.runs_tree.selection()
        if not sel or sel[0] == self.selected_run:
            return
        self.selected_run = sel[0]

        def work() -> None:
            try:
                self._results.put(("run", views.load_panel(self.transport, "runs", selected_run=self.selected_run)))
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=work, daemon=True).start()
        self.root.after(80, self._drain_run)

    def _drain_run(self) -> None:
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            self.root.after(80, self._drain_run)
            return
        if kind == "run":
            self._show_run(payload)
        else:                       # a full refresh landed first: apply it and keep waiting for ours
            self._busy = False
            self.apply(kind, payload)
            self.root.after(self.refresh_ms, self.refresh)
            self.root.after(80, self._drain_run)

    def _draw_gantt(self) -> None:
        c = self.gantt_canvas
        c.delete("all")
        width = max(200, c.winfo_width())
        used = charts.draw_gantt(TkPainter(c), width, self.gantt_data)
        c.config(height=max(120, min(int(used), 420)))

    def _apply_steps(self, d: dict) -> None:
        self.timing_rows = d["bars"]
        self._draw_timing()

    def _draw_timing(self) -> None:
        c = self.timing_canvas
        c.delete("all")
        charts.draw_timing(TkPainter(c), max(300, c.winfo_width()), self.timing_rows)

    def _apply_decisions(self, d: dict) -> None:
        tree = self.decisions_tree
        tree.delete(*tree.get_children())
        for x in d["decisions"]:
            tree.insert("", "end", values=(views.fmt_time(x["ts"]), x.get("run_id") or "", x.get("actor") or "",
                                           x.get("decision") or "", x.get("reason") or "",
                                           "" if x.get("confidence") is None else f"{x['confidence']:.2f}"))

    def _apply_workers(self, d: dict) -> None:
        tree = self.workers_tree
        tree.delete(*tree.get_children())
        for w in d["workers"]:
            tree.insert("", "end", tags=(w["state"],),
                        values=(w["worker"], w["state"], w["model"] or "-", "-" if w["queue"] is None else w["queue"],
                                w["detail"] or "", f"{w['age_s']}s ago"))

    def _apply_events(self, d: dict) -> None:
        if d["events"]:
            self.events_text.config(state="normal")
            for e in d["events"]:
                self.events_text.insert("end", views.event_line(e) + "\n")
            self.events_text.see("end")
            self.events_text.config(state="disabled")
        self.last_seq = d["last_seq"]

    def _apply_integrity(self, d: dict) -> None:
        c = d["chain"]
        self.integrity_label.config(
            text=(f"Event log verified: {c['count']} events, hash chain intact." if c["ok"]
                  else f"CHAIN BROKEN at event {c['first_bad_seq']}: {c['reason']}"),
            foreground="#1b7f45" if c["ok"] else "#b3261e")


def _dpi_aware() -> None:
    """Crisp text on scaled Windows displays (otherwise the window is bitmap-stretched)."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows, or already set
        pass


def launch(transport: Any, refresh_s: float = 2.0, tab: Optional[str] = None) -> int:
    _dpi_aware()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"no display available for the GUI ({exc}); use the terminal UI: python -m abp_cicd tui")
        return 1
    dash = Dashboard(root, transport, int(refresh_s * 1000))
    if tab in PANEL_IDS:
        dash.tabs.select(dash.frames[tab])
    dash.refresh()
    root.mainloop()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="abp-cicd-gui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["auto", "local", "http"], default="auto")
    p.add_argument("--db")
    p.add_argument("--url")
    p.add_argument("--token")
    p.add_argument("--refresh", type=float, default=2.0, help="seconds between refreshes")
    p.add_argument("--tab", choices=PANEL_IDS, help="open on this panel")
    args = p.parse_args(argv)
    return launch(choose(args.source, args.db, args.url, args.token), args.refresh, args.tab)


if __name__ == "__main__":
    raise SystemExit(main())
