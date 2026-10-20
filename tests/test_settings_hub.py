"""Settings (PLAN-local-chat.md P3): one shared file in both UIs, a Settings page pinned last in the navigation, the setup wizard
reachable as "Run setup again", every link pointing at something that exists, old links redirected - and nothing lost: every
page, nav link and named control that existed before Local Chat and Settings (tests/data/ui_inventory.json) is still there."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB, DESK = ROOT / "bot/dashboard/static", ROOT / "desktop-app/ui"
PAGES = {"bot/dashboard/static/dashboard.html": "/static/", "desktop-app/ui/index.html": ""}
INVENTORY = json.loads((ROOT / "tests/data/ui_inventory.json").read_text(encoding="utf-8"))
JS = (DESK / "settings-hub.js").read_text(encoding="utf-8")


def _html(page: str) -> str:
    return (ROOT / page).read_text(encoding="utf-8")


def test_the_panel_is_identical_in_both_uis():
    a, b = (WEB / "settings-hub.js").read_bytes(), (DESK / "settings-hub.js").read_bytes()
    assert a == b and len(a) > 3000


@pytest.mark.parametrize("page,prefix", PAGES.items())
def test_settings_is_a_page_pinned_last_in_the_navigation(page, prefix):
    html = _html(page)
    assert '<section id="settings">' in html and 'id="settings-root"' in html
    assert f'<script src="{prefix}settings-hub.js"></script>' in html
    nav = html[html.index('<nav class="sidenav"'):html.index("</nav>", html.index('<nav class="sidenav"'))]
    links = re.findall(r'<a href="#([\w-]+)"', nav)
    assert links[-1] == "settings" and 'class="nav-settings"' in nav


@pytest.mark.parametrize("page", list(PAGES))
def test_nothing_that_existed_before_is_lost(page):
    html = _html(page)
    before = INVENTORY[page]
    sections = set(re.findall(r'<section id="([\w-]+)"', html))
    nav = html[html.index('<nav class="sidenav"'):html.index("</nav>", html.index('<nav class="sidenav"'))]
    links = set(re.findall(r'<a href="#([\w-]+)"', nav))
    controls = set(re.findall(r'<(?:input|select|textarea|button)\b[^>]*\bid="([\w-]+)"', html))
    assert not set(before["sections"]) - sections, "pages removed"
    assert not set(before["nav"]) - links, "nav links removed"
    assert not set(before["controls"]) - controls, "controls removed"
    assert len(before["sections"]) >= 50 and len(before["controls"]) >= 200      # the inventory is the real list


def _index() -> list[dict]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = """
const fs = require('fs');
global.window = { addEventListener() {} };
global.document = { readyState: 'complete', getElementById: () => null, addEventListener() {}, querySelector: () => null, head: { appendChild() {} }, createElement: () => ({}) };
global.api = async () => ({});
eval(fs.readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify(window.ABP.settingsIndex));
"""
    r = subprocess.run([node, "-e", harness, str(DESK / "settings-hub.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_sections_are_the_planned_ones_and_every_link_resolves():
    idx = _index()
    assert [s["title"] for s in idx] == ["Backends", "Local models", "Cloud & capacity", "Agent", "Factory", "Safety",
                                         "Platforms & bots", "Devices & linked servers", "Storage", "Appearance & shortcuts",
                                         "Updates", "Advanced"]
    ids = {p: set(re.findall(r'\bid="([\w-]+)"', _html(p))) for p in PAGES}
    for sec in idx:
        for item in sec["items"]:
            t = item["target"]
            if t.startswith("do:"):
                assert t in ("do:wizard", "do:shortcuts", "do:estop"), t
                continue
            tid = t[5:] if t.startswith("card:") else t
            if tid.startswith("sh-"):
                assert f'id="{tid}"' in JS                                       # drawn by the hub itself
                continue
            alt = item["alt"]
            found = [p for p in PAGES if tid in ids[p] or (alt and alt in ids[p])]
            assert found, f"{sec['title']} -> {item['label']} points at {t}, which neither app has"


def test_every_page_that_existed_before_is_reachable_from_settings_or_the_navigation():
    idx = _index()
    targets = {i["target"] for s in idx for i in s["items"]} | {i["alt"] for s in idx for i in s["items"] if i["alt"]}
    for page in PAGES:
        nav = _html(page)
        nav = nav[nav.index('<nav class="sidenav"'):nav.index("</nav>", nav.index('<nav class="sidenav"'))]
        links = set(re.findall(r'<a href="#([\w-]+)"', nav))
        for sec in INVENTORY[page]["sections"]:
            assert sec in links or sec in targets, (page, sec)
    # the settings-like pages are all indexed
    for must in ("control", "models", "permissions", "agents", "updates", "platforms", "mobile", "servers", "database",
                 "customize", "coordination", "modules"):
        assert must in targets, must


def test_the_wizard_is_run_setup_again_and_old_links_redirect():
    assert "'Run setup again'" in JS and "do:wizard" in JS and "$('btn-open-wizard')" in JS
    redirects = re.search(r"const REDIRECTS = \{([^}]*)\}", JS).group(1)
    for old in ("setup", "wizard", "backends", "preferences", "config"):
        assert re.search(rf"\b'?{old}'?:", redirects), old
    assert "history.replaceState(null, '', '#settings/'" in JS and "h.startsWith('settings/')" in JS
    for page in PAGES:                                                     # the wizard markup and its opener are still there
        assert 'id="wizard"' in _html(page) and 'id="btn-open-wizard"' in _html(page)


def test_local_chat_defaults_are_edited_through_the_config_api():
    assert "'/api/config/set'" in JS and "path: ['local_chat', t.dataset.shKey]" in JS
    from bot import localchat

    keys = set(re.findall(r'data-sh-key="(\w+)"', JS))
    assert keys and keys <= set(localchat.DEFAULTS), keys - set(localchat.DEFAULTS)


def test_settings_search_and_escaping():
    assert 'id="sh-search"' in JS and "function matches(" in JS
    interpolations = re.findall(r"\$\{([^{}]*)\}", JS)
    risky = [i for i in interpolations if re.search(r"\b(?:label|desc|title|version|degraded|name)\b", i) and "E(" not in i]
    assert not risky, risky
