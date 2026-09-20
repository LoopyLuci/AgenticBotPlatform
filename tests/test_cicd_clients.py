"""The interactive clients (terminal UI and desktop GUI) and the guarantee that
they show the same things: shared view models, chart geometry, and parity."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from abp_cicd import charts, cli, recorder, service, transport, views
from abp_cicd.store import EventStore


@pytest.fixture
def store():
    return EventStore(os.environ["ABP_CICD_DB"])


@pytest.fixture
def seeded(store):
    with recorder.start_run("pipeline", store=store, title="pre-push") as run:
        run.record_step("python", "ok", 300_000)
        run.record_step("rust", "skipped", 0, skipped_reason="no changes")
        run.record_step("android", "failed", 45_000, error="Boom")
        run.decision(actor="rules", decision="skip rust", reason="no src-tauri changes", confidence=1.0)
        run.finish("failed", "android failed")
    store.append("worker.heartbeat", {"worker": "flake_detector", "state": "serving", "model": "v2", "queue": 1})
    return run


@pytest.fixture
def t(store):
    return transport.LocalTransport(store.path)


# ---- parity: the guarantee -------------------------------------------------- #
def test_every_service_capability_is_shown_by_some_panel():
    assert views.uncovered_capabilities() == []


def test_the_terminal_ui_has_exactly_the_panels_the_views_define():
    from abp_cicd import tui
    assert tui.PANEL_IDS == tuple(views.PANELS)
    for pid in views.PANELS:
        assert hasattr(tui.DashboardApp, f"_apply_{pid}"), f"the TUI cannot render the {pid} panel"


def test_the_desktop_gui_has_exactly_the_panels_the_views_define():
    from abp_cicd import gui
    assert gui.PANEL_IDS == tuple(views.PANELS)
    for pid in views.PANELS:
        assert hasattr(gui.Dashboard, f"_build_{pid}") and hasattr(gui.Dashboard, f"_apply_{pid}"), pid


def test_every_panel_loads_through_the_shared_loader(t, seeded):
    for pid in views.PANELS:
        assert isinstance(views.load_panel(t, pid), dict), pid


def test_the_cli_can_launch_both_interactive_clients():
    commands = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert {"tui", "gui"} <= commands


# ---- view models ------------------------------------------------------------ #
def test_gantt_positions_steps_on_a_shared_axis_and_marks_the_slowest():
    run = {"started": 1000.0, "duration_ms": 400_000, "steps": [
        {"name": "a", "status": "ok", "started": 1000.0, "duration_ms": 100_000},
        {"name": "b", "status": "ok", "started": 1100.0, "duration_ms": 300_000},
        {"name": "c", "status": "skipped", "started": None, "duration_ms": 0}]}
    g = views.gantt(run)
    assert g["total_ms"] == 400_000
    assert [b["offset_ms"] for b in g["bars"]] == [0, 100_000, 400_000]
    assert [b["slowest"] for b in g["bars"]] == [False, True, False]


def test_gantt_handles_steps_that_only_recorded_a_duration():
    g = views.gantt({"started": 5.0, "steps": [{"name": "x", "status": "ok", "duration_ms": 10},
                                               {"name": "y", "status": "ok", "duration_ms": 20}]})
    assert [b["offset_ms"] for b in g["bars"]] == [0, 10]


def test_gantt_of_a_running_step_uses_the_elapsed_time():
    g = views.gantt({"started": 100.0, "steps": [{"name": "x", "status": "running", "started": 100.0,
                                                  "duration_ms": None}]}, now=104.0)
    assert g["bars"][0]["duration_ms"] == 4000


def test_overview_tiles_summarise_health(t, seeded):
    tiles = {x["title"]: x for x in views.overview_tiles(t.summary())}
    assert tiles["Pipeline"]["value"] == "failed" and tiles["Pipeline"]["status"] == "failed"
    assert tiles["Release"]["value"] == "no runs"
    assert tiles["Log integrity"]["value"] == "verified"
    assert tiles["ML workers"]["value"] == "1 active"


def test_timing_bars_are_sorted_slowest_first(t, seeded):
    names = [b["name"] for b in views.timing_bars(t.step_stats()["steps"])]
    assert names[0] == "python"


def test_the_runs_panel_selects_the_newest_run_by_default(t, seeded):
    d = views.load_panel(t, "runs")
    assert d["selected"] == seeded.id and d["run"]["kind"] == "pipeline" and "android" in d["explain"]
    assert views.load_panel(t, "runs", selected_run="missing")["run"] is None


def test_the_events_panel_only_returns_new_events_after_a_cursor(t, seeded):
    first = views.load_panel(t, "events")
    assert first["events"] and views.load_panel(t, "events", since=first["last_seq"])["events"] == []


# ---- charts (toolkit-independent) ------------------------------------------- #
def test_gantt_chart_geometry_is_proportional():
    p = charts.RecordingPainter()
    g = {"total_ms": 1000, "bars": [{"name": "a", "offset_ms": 0, "duration_ms": 250, "status": "ok", "slowest": False},
                                    {"name": "b", "offset_ms": 250, "duration_ms": 750, "status": "failed", "slowest": True}]}
    charts.draw_gantt(p, 800, g)
    bars = p.rects()
    assert len(bars) == 2
    wa, wb = bars[0][3], bars[1][3]
    assert abs(wb / wa - 3.0) < 0.01                       # 750ms vs 250ms
    assert bars[1][1] > bars[0][1]                          # b starts after a
    assert {"a", "b"} <= set(p.texts())
    assert bars[1][5] == charts.PALETTE["slowest"]


def test_charts_say_so_when_there_is_nothing_to_show():
    p = charts.RecordingPainter()
    charts.draw_gantt(p, 500, {"total_ms": 0, "bars": []})
    charts.draw_timing(p, 500, [])
    assert "no steps recorded" in p.texts()[0] and "no step timings" in p.texts()[1]


def test_timing_chart_draws_p95_behind_p50():
    p = charts.RecordingPainter()
    charts.draw_timing(p, 800, [{"name": "python", "p50_ms": 100, "p95_ms": 400, "max_ms": 500, "n": 9, "failed": 1,
                                 "last_status": "ok"}])
    wide, narrow = p.rects()
    assert wide[3] > narrow[3] * 3
    assert any("1 failed" in s for s in p.texts())


# ---- terminal UI ------------------------------------------------------------ #
def _run_tui(t, body):
    from abp_cicd.tui import DashboardApp

    async def go():
        app = DashboardApp(t, refresh_s=3600)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app.refresh_all()
            await pilot.pause()
            return await body(app, pilot)
    return asyncio.run(go())


def test_the_tui_shows_every_panel_with_real_data(t, seeded):
    from textual.widgets import DataTable, Static

    async def body(app, pilot):
        overview = str(app.query_one("#overview-body", Static).render())
        assert "Pipeline" in overview and "failed" in overview and "verified" in overview
        assert app.query_one("#runs-table", DataTable).row_count == 1
        assert "android" in str(app.query_one("#runs-detail", Static).render())
        assert "python" in str(app.query_one("#steps-body", Static).render())
        assert app.query_one("#decisions-table", DataTable).row_count == 1
        assert app.query_one("#workers-table", DataTable).row_count == 1
        assert app.events_shown >= 5
        assert "hash chain intact" in str(app.query_one("#integrity-body", Static).render())
    _run_tui(t, body)


def test_the_tui_switches_tabs_with_number_keys(t, seeded):
    from textual.widgets import TabbedContent

    async def body(app, pilot):
        await pilot.press("5")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "workers"
    _run_tui(t, body)


def test_the_tui_survives_an_unreachable_server_and_says_so():
    from abp_cicd.tui import DashboardApp

    async def go():
        app = DashboardApp(transport.HttpTransport("http://127.0.0.1:1", "x"), refresh_s=3600)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.refresh_all()
            assert "disconnected" in app.sub_title and app.errors >= 1
    asyncio.run(go())


def test_the_tui_only_appends_new_events(t, seeded, store):
    async def body(app, pilot):
        before = app.events_shown
        await app.refresh_all()
        assert app.events_shown == before                    # nothing new -> nothing added
        store.append("note", {"message": "later"}, run_id="x")
        await app.refresh_all()
        assert app.events_shown == before + 1
    _run_tui(t, body)


# ---- desktop GUI ------------------------------------------------------------ #
@pytest.fixture
def tk_root():
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_the_gui_builds_every_tab_and_fills_them_with_real_data(t, seeded, tk_root):
    from abp_cicd import gui
    dash = gui.Dashboard(tk_root, t)
    assert [dash.tabs.tab(i, "text") for i in range(dash.tabs.index("end"))] == [v[0] for v in views.PANELS.values()]
    dash.apply("ok", dash._fetch_all())
    tk_root.update_idletasks()
    assert len(dash.runs_tree.get_children()) == 1
    assert len(dash.decisions_tree.get_children()) == 1
    assert len(dash.workers_tree.get_children()) == 1
    assert "android" in dash.explain_text.get("1.0", "end")
    assert "hash chain intact" in dash.integrity_label.cget("text")
    assert "connected" in dash.status.get()
    assert dash.gantt_data["bars"][0]["name"] == "python"
    assert "python" in dash.events_text.get("1.0", "end") or "run.start" in dash.events_text.get("1.0", "end")
    assert dash.tiles.winfo_children()                      # overview tiles were created


def test_the_gui_draws_the_charts_on_real_canvases(t, seeded, tk_root):
    from abp_cicd import gui
    dash = gui.Dashboard(tk_root, t)
    dash.apply("ok", dash._fetch_all())
    tk_root.update_idletasks()
    assert dash.gantt_canvas.find_all() and dash.timing_canvas.find_all()


def test_the_gui_reports_a_disconnected_server_without_crashing(tk_root):
    from abp_cicd import gui
    dash = gui.Dashboard(tk_root, transport.HttpTransport("http://127.0.0.1:1", "x"))
    dash.apply("offline", "can't reach http://127.0.0.1:1")
    assert "disconnected" in dash.status.get()
    dash.apply("error", "ValueError: x")
    assert "refresh failed" in dash.status.get()


def test_the_gui_refresh_runs_off_the_ui_thread_and_applies_results(t, seeded, tk_root):
    from abp_cicd import gui
    dash = gui.Dashboard(tk_root, t, refresh_ms=3_600_000)
    dash.refresh()
    deadline = time.time() + 15
    while "connected" not in dash.status.get() and time.time() < deadline:
        tk_root.update()
        time.sleep(0.05)
    assert "connected" in dash.status.get() and len(dash.runs_tree.get_children()) == 1
    dash.close()


def test_the_gui_shows_an_empty_store_cleanly(store, tk_root):
    from abp_cicd import gui
    dash = gui.Dashboard(tk_root, transport.LocalTransport(store.path))
    dash.apply("ok", dash._fetch_all())
    assert dash.runs_tree.get_children() == () and "no runs" in dash.explain_text.get("1.0", "end")
