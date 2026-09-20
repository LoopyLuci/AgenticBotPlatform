"""The Terminal/Activity bar must BE the bottom of the window.

It used to be `position:fixed` over a page that scrolled as a whole, with a
`padding-bottom` guess to keep content clear of it — so content near the
bottom, and the lower items of the sidebar, could sit underneath it. The
window is now a flex column: [ shell = sidebar + main, each scrolling inside
itself ] over [ the bar ]. These tests pin the structure (they can't run a
browser; the geometry itself was verified live at 375 to 2560px wide, in
every bar state, in both UIs).
"""
from __future__ import annotations

import re

import pytest

from bot.envfile import CODE_ROOT

UIS = {
    "dashboard": "bot/dashboard/static/dashboard.html",
    "desktop": "desktop-app/ui/index.html",
}


def _css(name: str) -> str:
    return (CODE_ROOT / UIS[name]).read_text(encoding="utf-8")


def _rule(css: str, selector_line_start: str) -> str:
    """The body of the first CSS rule whose line starts with the selector."""
    match = re.search(re.escape(selector_line_start) + r"\s*\{(.*?)\}", css, re.DOTALL)
    assert match, f"no CSS rule for {selector_line_start!r}"
    return match.group(1)


@pytest.mark.parametrize("ui", UIS)
def test_the_window_is_a_flex_column_that_never_scrolls_as_a_whole(ui):
    body = _rule(_css(ui), "  body")
    assert "display:flex" in body and "flex-direction:column" in body
    assert "overflow:hidden" in body
    assert "height:100vh" in body


@pytest.mark.parametrize("ui", UIS)
def test_the_bar_is_a_row_in_the_layout_not_an_overlay(ui):
    panel = _rule(_css(ui), "  .term-panel")
    assert "position:fixed" not in panel
    assert "position:relative" in panel and "flex:none" in panel
    assert "translateY" not in _css(ui).split(".term-panel.collapsed", 1)[1].split("\n", 1)[0]


@pytest.mark.parametrize("ui", UIS)
def test_collapsed_and_maximized_beat_an_inline_height_from_dragging(ui):
    css = _css(ui)
    assert "term-panel.collapsed { height:40px !important" in css
    assert "term-panel.maximized { height:92vh !important" in css


@pytest.mark.parametrize("ui", UIS)
def test_sidebar_and_content_scroll_inside_themselves_and_end_above_the_bar(ui):
    css = _css(ui)
    shell = _rule(css, "  .shell")
    assert "flex:1 1 auto" in shell and "min-height:0" in shell and "min-height:100vh" not in shell
    side = re.search(r"  \.side \{([^}]*)\}", css).group(1)
    assert "position:sticky" not in side and "height:100vh" not in side
    assert "overflow-y:auto" in side and "min-height:0" in side
    main = _rule(css, "  main")
    assert "overflow:auto" in main and "min-height:0" in main
    assert "--term-clearance" not in css, "the padding-bottom guess must be gone"


@pytest.mark.parametrize("ui", UIS)
def test_main_can_take_keyboard_focus_so_page_keys_scroll_it(ui):
    assert '<main tabindex="-1">' in _css(ui)


@pytest.mark.parametrize("path", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/main.js"])
def test_jump_to_section_scrolls_main_not_the_window(path):
    source = (CODE_ROOT / path).read_text(encoding="utf-8")
    assert "window.scrollTo" not in source and "window.scrollY" not in source
    assert "scroller.scrollTo" in source
    assert "querySelector('main').addEventListener('scrollend'" in source


@pytest.mark.parametrize("path", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/main.js"])
def test_toasts_stack_above_the_bar_not_on_it(path):
    source = (CODE_ROOT / path).read_text(encoding="utf-8")
    assert "bottom:calc(var(--term-panel-h, 40px) + 20px)" in source


def test_terminal_panel_script_tracks_its_height_and_is_identical_in_both_apps():
    dash = (CODE_ROOT / "bot/dashboard/static/terminal-panel.js").read_text(encoding="utf-8")
    desk = (CODE_ROOT / "desktop-app/ui/terminal-panel.js").read_text(encoding="utf-8")
    assert dash == desk
    assert "--term-panel-h" in dash and "ResizeObserver" in dash
    assert "--term-clearance" not in dash


# ------------------------------------------------- responsive safety net
@pytest.mark.parametrize("ui", UIS)
def test_responsive_safety_net_is_present(ui):
    css = _css(ui)
    assert "Responsive safety net" in css
    assert "select, textarea, input:not([type=checkbox])" in css and "max-width:100%" in css
    assert "main { overflow-wrap:anywhere; }" in css
    assert ".wizard-field .row { flex-wrap:wrap; }" in css
    assert "@media (max-width:980px) {\n    table { display:block; max-width:100%; overflow-x:auto; }" in css.replace("\r\n", "\n")


@pytest.mark.parametrize("ui", UIS)
def test_terminal_bar_header_fits_a_phone(ui):
    css = _css(ui).replace("\r\n", "\n")
    assert ".term-header { overflow:hidden; }" in css
    phone = css.split("@media (max-width:640px) {", 1)[1].split("}\n  }", 1)[0]
    assert ".term-instance-select { display:none; }" in phone
    assert ".term-tab { padding:0 10px; }" in phone


def test_desktop_chat_reflows_on_narrow_windows():
    css = _css("desktop").replace("\r\n", "\n")
    assert ".chat-convo-header { flex-wrap:wrap;" in css
    narrow = css.split("@media (max-width:760px) {\n    .chat-shell", 1)[1]
    assert "flex-direction:column" in narrow.split("}", 1)[0]
