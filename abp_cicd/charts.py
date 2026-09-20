"""Chart drawing, independent of any GUI toolkit.

The charts draw through a tiny `Painter` interface (rect / text). The Tkinter GUI
supplies a painter backed by a Canvas; tests use `RecordingPainter` and check
the geometry. Swapping the toolkit (e.g. for Qt) means writing one new painter,
not new charts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .views import STATUS_STYLE, fmt_ms

PALETTE = {"green": "#2e9e5b", "red": "#d9483b", "grey": "#9aa0a6", "blue": "#3b82d9", "orange": "#e0902a",
           "text": "#202124", "axis": "#c9ccd1", "slowest": "#7c3aed"}


class Painter(Protocol):
    def rect(self, x: float, y: float, w: float, h: float, fill: str, outline: str = "") -> None: ...

    def text(self, x: float, y: float, s: str, fill: str = "#202124", anchor: str = "w", bold: bool = False) -> None: ...


@dataclass
class RecordingPainter:
    """Collects draw operations, for tests and for exporting a chart as data."""
    ops: list[tuple] = field(default_factory=list)

    def rect(self, x, y, w, h, fill, outline=""):
        self.ops.append(("rect", x, y, w, h, fill, outline))

    def text(self, x, y, s, fill="#202124", anchor="w", bold=False):
        self.ops.append(("text", x, y, s, fill, anchor, bold))

    def rects(self):
        return [o for o in self.ops if o[0] == "rect"]

    def texts(self):
        return [o[3] for o in self.ops if o[0] == "text"]


def colour(status: str) -> str:
    return PALETTE.get(STATUS_STYLE.get(status, "grey"), PALETTE["grey"])


LABEL_W = 150
ROW_H = 26
PAD = 10


def draw_gantt(p: Painter, width: float, g: dict) -> float:
    """A step-timeline: one row per step, bar positioned and sized on a shared
    time axis. Returns the height used."""
    bars, total = g["bars"], max(1, g["total_ms"])
    plot_w = max(50.0, width - LABEL_W - 2 * PAD - 70)
    if not bars:
        p.text(PAD, PAD + 8, "no steps recorded", PALETTE["grey"])
        return 2 * PAD + 16
    for i, b in enumerate(bars):
        y = PAD + i * ROW_H
        p.text(PAD, y + ROW_H / 2, b["name"], PALETTE["text"])
        x = LABEL_W + PAD + plot_w * b["offset_ms"] / total
        w = max(2.0, plot_w * b["duration_ms"] / total)
        p.rect(x, y + 4, w, ROW_H - 8, PALETTE["slowest"] if b["slowest"] else colour(b["status"]),
               PALETTE["slowest"] if b["slowest"] else "")
        p.text(x + w + 6, y + ROW_H / 2, fmt_ms(b["duration_ms"]) if b["status"] != "skipped" else "skipped",
               PALETTE["grey"])
    axis_y = PAD + len(bars) * ROW_H + 4
    p.text(LABEL_W + PAD, axis_y + 8, "0", PALETTE["grey"])
    p.text(LABEL_W + PAD + plot_w, axis_y + 8, fmt_ms(total), PALETTE["grey"], anchor="e")
    return axis_y + 22


def draw_timing(p: Painter, width: float, rows: list[dict]) -> float:
    """Per step: a bar for p50, a longer lighter bar for p95, and the counts."""
    if not rows:
        p.text(PAD, PAD + 8, "no step timings recorded yet", PALETTE["grey"])
        return 2 * PAD + 16
    scale = max(1, max(r["p95_ms"] or r["p50_ms"] for r in rows))
    plot_w = max(50.0, width - LABEL_W - 2 * PAD - 260)
    for i, r in enumerate(rows):
        y = PAD + i * ROW_H
        p.text(PAD, y + ROW_H / 2, r["name"], PALETTE["text"])
        p.rect(LABEL_W + PAD, y + 4, max(2.0, plot_w * r["p95_ms"] / scale), ROW_H - 8, "#c7d7f2")
        p.rect(LABEL_W + PAD, y + 4, max(2.0, plot_w * r["p50_ms"] / scale), ROW_H - 8, PALETTE["blue"])
        note = f"p50 {fmt_ms(r['p50_ms'])}  p95 {fmt_ms(r['p95_ms'])}  n={r['n']}"
        if r["failed"]:
            note += f"  {r['failed']} failed"
        p.text(LABEL_W + PAD + plot_w + 8, y + ROW_H / 2, note, PALETTE["red"] if r["failed"] else PALETTE["grey"])
    return PAD * 2 + len(rows) * ROW_H
