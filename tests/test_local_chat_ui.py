"""Local Chat's screen (PLAN-local-chat.md P2): one shared file pair (local-chat.js + local-chat.css) loaded identically by both
UIs, the first section and the first nav entry, escaping everything it draws, accessible controls, and only calling routes
that exist."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
WEB, DESK = ROOT / "bot/dashboard/static", ROOT / "desktop-app/ui"
PAGES = {"bot/dashboard/static/dashboard.html": "/static/", "desktop-app/ui/index.html": ""}
JS = (DESK / "local-chat.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["local-chat.js", "local-chat.css"])
def test_the_file_pair_is_identical_in_both_uis(name):
    a, b = (WEB / name).read_bytes(), (DESK / name).read_bytes()
    assert a == b and len(a) > 3000, name


@pytest.mark.parametrize("page,prefix", PAGES.items())
def test_both_pages_load_it_and_put_local_chat_first(page, prefix):
    html = (ROOT / page).read_text(encoding="utf-8")
    assert f'<link rel="stylesheet" href="{prefix}local-chat.css">' in html
    assert f'<script src="{prefix}local-chat.js"></script>' in html
    assert html.index(f"{prefix}local-chat.js") > html.index("main.js" if not prefix else "dashboard.js")   # after api()
    sections = re.findall(r'<section id="([\w-]+)"', html)
    assert sections[:2] == ["local-chat", "overview"], sections[:3]
    nav = html[html.index('<nav class="sidenav"'):]
    links = re.findall(r'<a href="#([\w-]+)"', nav)
    assert links[:2] == ["local-chat", "overview"]
    assert 'id="lc-root"' in html


@pytest.mark.parametrize("js", ["bot/dashboard/static/dashboard.js", "desktop-app/ui/main.js"])
def test_the_page_forwards_socket_frames_and_boot_ends_in_local_chat(js):
    text = (ROOT / js).read_text(encoding="utf-8")
    assert "new CustomEvent('abp:ws-frame', { detail: msg })" in text
    assert "history.replaceState(null, '', '#local-chat')" in text
    assert "landOnLocalChat();\r\n  refreshAll();" in text or "landOnLocalChat();\n  refreshAll();" in text


def test_everything_drawn_from_the_server_goes_through_the_escaper():
    risky = re.compile(r"\b(?:s|m|r|a|p|t|pv|row|md0|st|hub|c|d)\.(?:title|text|name|id|model|workspace|tool|summary|body|"
                       r"device|version|degraded|mode|preview|approval_id|sid|kind|role)\b")
    safe = ("E(", "md(", "badge(", "inline(", "shortModel(", "fmtTps(", "when(", "gb(")
    js = re.sub(r"function asMarkdown\(p\) \{.*?\n  \}\n", "", JS, flags=re.S)       # plain-text Markdown export, not HTML

    def unsafe(expr: str) -> bool:
        if any(f in expr for f in safe) or "===" in expr or "!==" in expr:
            return False
        shown = expr.split("?", 1)[1] if "?" in expr else expr        # a condition decides; only the branches are drawn
        return bool(risky.search(shown))
    bad = [i for i in re.findall(r"\$\{([^{}]*)\}", js) if unsafe(i)]
    assert not bad, bad
    assert "innerHTML" in JS and "const E = (s)" in JS


def test_every_generated_control_has_a_name():
    for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", JS, re.S):
        attrs, text = m.group(1), re.sub(r"<[^>]+>|\$\{[^}]*\}", "x", m.group(2)).strip()
        assert text or "aria-label" in attrs or "title=" in attrs, m.group(0)[:120]
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>", JS):
        if 'type="checkbox"' in m.group(2):
            continue                                            # wrapped in its <label>
        assert "aria-label" in m.group(2), m.group(0)[:120]


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_markdown_renderer_escapes_and_only_makes_safe_links():
    harness = """
const fs = require('fs');
global.window = { addEventListener() {}, dispatchEvent() {} };
global.document = { readyState: 'complete', getElementById: () => null, addEventListener() {}, querySelector: () => null };
global.localStorage = { getItem: () => null, setItem() {} };
global.api = async () => ({});
eval(fs.readFileSync(process.argv[1], 'utf8'));
const md = window.ABP.localChatMarkdown;
const cases = {
  script: md('<script>alert(1)</script> and **bold** `x<y`'),
  link: md('[ok](https://example.org/a) [bad](javascript:alert(1))'),
  code: md('```py\\nprint("<b>")\\n```'),
  list: md('- one\\n- two\\n\\n1. a\\n2. b'),
  open: md('```\\nstill streaming <i>'),
};
process.stdout.write(JSON.stringify(cases));
"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "-e", harness, str(DESK / "local-chat.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "<script>" not in out["script"] and "&lt;script&gt;" in out["script"]
    assert "<b>bold</b>" in out["script"] and "<code>x&lt;y</code>" in out["script"]
    assert '<a href="https://example.org/a"' in out["link"] and "javascript:" not in out["link"].split("[bad]")[0].replace("[bad]", "")
    assert 'href="javascript' not in out["link"]
    assert "print(&quot;&lt;b&gt;&quot;)" in out["code"] and 'data-lc="copy-code"' in out["code"]
    assert "<ul><li>one</li><li>two</li></ul>" in out["list"] and "<ol><li>a</li><li>b</li></ol>" in out["list"]
    assert "&lt;i&gt;" in out["open"]                                     # an unfinished fence while streaming is still code


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


def _route_exists(app, method: str, path: str) -> bool:
    from starlette.routing import Match

    scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
    return any(r.matches(scope)[0] == Match.FULL for r in app.routes)


def test_the_panel_only_calls_routes_that_exist(client):
    calls = set(re.findall(r"'(/api/[a-z0-9/_-]+)", JS))
    assert "/api/local-chat/sessions" in calls and "/api/local-chat/models" in calls
    mm = (ROOT / "bot/dashboard/modelmistress_api.py").read_text(encoding="utf-8")     # an optional unit's routes
    assert '"/api/modelmistress/load"' in mm and '"/api/modelmistress/unload"' in mm
    calls.discard("/api/modelmistress/")
    for path in sorted(calls):
        probes = [path + "x", path + "x/approve"] if path.endswith("/") else [path]      # a prefix the panel completes
        methods = ("GET", "POST", "PATCH", "DELETE")
        assert any(_route_exists(client.app, m, pr) for m in methods for pr in probes), path
    for name in ("local-chat.js", "local-chat.css"):
        assert client.get("/static/" + name).status_code == 200


def test_the_registry_has_the_screen_and_its_actions():
    reg = (DESK / "action-registry.js").read_text(encoding="utf-8")
    assert "[['local-chat'], 'Local Chat', 'lc']" in reg
    assert "id: 'localchat.new'" in reg and "id: 'localchat.focus'" in reg


# ---- S2: the sidebar, the session header, split view, notifications --------------------------------------------------------
def test_the_sidebar_sorts_sessions_into_sections():
    harness = """
const fs = require('fs');
global.window = { addEventListener() {}, dispatchEvent() {} };
global.document = { readyState: 'complete', getElementById: () => null, addEventListener() {}, querySelector: () => null };
global.localStorage = { getItem: () => null, setItem() {} };
global.api = async () => ({});
eval(fs.readFileSync(process.argv[1], 'utf8'));
const lc = window.ABP.localChat;
lc.state.groups = [{ id: 'g1', name: 'Factory' }];
const now = Date.now() / 1000;
const rows = [
  { id: 'a', pinned: true, status: 'working', updated: now },
  { id: 'b', status: 'needs_you', group_id: 'g1', updated: now },
  { id: 'c', status: 'working', updated: now },
  { id: 'd', status: 'done', group_id: 'g1', updated: now },
  { id: 'e', status: 'idle', group_id: 'gone', updated: now },
  { id: 'f', status: 'done', updated: now - 3 * 86400 },
];
const out = {};
rows.forEach((r) => { out[r.id] = lc.sectionOf(r); });
lc.state.showArchived = true;
out.arch = lc.sectionOf(rows[5]);
process.stdout.write(JSON.stringify(out));
"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "-e", harness, str(DESK / "local-chat.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"a": "pinned", "b": "needs", "c": "working", "d": "g:g1", "e": "today", "f": "earlier",
                                    "arch": "archived"}


def test_the_sidebar_header_and_composer_offer_the_session_actions():
    for label in ("'Pinned'", "'Needs you'", "'Working now'", "'Today'", "'Earlier'", "'Archived'"):
        assert label in JS, label
    for action in ("wt-diff", "wt-sync", "wt-commit", "wt-merge", "wt-keep", "wt-discard", "wt-create", "rewind", "queue",
                   "group-new", "split", "notify", "prune"):
        assert f'"{action}"' in JS or f"'{action}'" in JS, action
    for route in ("/worktree", "'/diff'", "'/sync'", "'/commit-message'", "'/commit'", "'/queue-merge'", "'/keep'", "'/discard'",
                  "'/steer'", "'/rewind'", "'/read'", "/api/local-chat/groups", "/api/local-chat/worktrees/prune"):
        assert route in JS, route
    assert "new Notification(" in JS and "Notification.requestPermission()" in JS
    assert "lc-split" in JS and "openSplit" in JS
    # a 409 from discard / delete is shown with what would be lost before anything is removed
    assert JS.count("would_lose") >= 4 and "{ confirm: true }" in JS and "?confirm=true" in JS


# ---- S3: linked sessions -------------------------------------------------------------------------------------------------
def test_linked_sessions_are_offered_nested_and_noted():
    for action in ("['fork', ", "['task', ", "['handoff', "):
        assert action in JS, action
    assert "url + '/' + action" in JS                       # POST .../fork | /task | /handoff
    assert "return 'child';" in JS and "function sessionRows(" in JS        # children drawn under their parent
    assert "'/children'" in JS and "local_chat_note" in JS
    assert "m.meta && m.meta.child" in JS                   # a note in the parent opens the child
    assert "p.session.link === 'worker'" in JS and ".disabled = ro" in JS    # workers are read-only


# ---- P4: agent and Factory in chat -----------------------------------------------------------------------------------------
def test_factory_cloud_assist_and_the_right_panel_are_wired():
    assert "['factory', 'Factory'," in JS
    assert "'/factory/run'" in JS and "confirm: !!confirmed" in JS and "d.needs_confirmation" in JS
    assert "'/api/conductor/queue/'" in JS and "(approve ? 'approve' : 'reject')" in JS     # approve from the chat (never pushes)
    assert "cloud ? { text, cloud: true } : { text }" in JS and "assist === 'off'" in JS
    for route in ("'/context'", "'/factory'", "/api/manager/digest", "/api/manager/escalations", "/api/redblue/summary"):
        assert route in JS, route
    assert 'role="meter"' in JS and "badge('estimated')" in JS


# ---- S4: long sessions ------------------------------------------------------------------------------------------------------
def test_long_session_ui_is_wired():
    assert "mm.compaction" in JS and "lc-compacted" in JS and "mm.chapter" in JS and "'/chapters'" in JS
    assert "mm.steps" in JS and "mm.next" in JS and 'data-lc="answer"' in JS
    assert "'/compact'" in JS and "/export?format=" in JS and "local_chat_compacted" in JS
