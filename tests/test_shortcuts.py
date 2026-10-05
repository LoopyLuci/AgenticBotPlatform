"""Keyboard shortcuts, the command palette and right-click menus for both ABP UIs.

Two halves, tested two ways.

The JS half runs the real registry and the real engine in node: action-registry.js and
shortcuts.js are loaded with a stubbed window/document (they only touch those inside handlers),
so the duplicate-id, duplicate-chord, reserved-key and referential-integrity checks run against
the actual data the browser will run - not against a copy of it in Python. node --check is run
over both files and both UIs' copies.

The API half is a round trip against the real FastAPI app: a binding PUT through the real route
lands in config/backends.yaml and comes back through GET, survives a restart of the app, and a
refused PUT changes nothing. The validation the browser does is duplicated on the server on
purpose - that is the real gate - and tests/test_shortcuts.py checks the two reserved-chord lists
have not drifted apart.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.config import config
from bot.dashboard import shortcuts_api
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "bot/dashboard/static"
DESK = ROOT / "desktop-app/ui"
PAGES = {"dashboard": DASH / "dashboard.html", "desktop": DESK / "index.html"}
SHARED_JS = ["action-registry.js", "shortcuts.js"]

H = {"X-Dashboard-Token": "test-token"}
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is needed to run the real registry")


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


# ---- loading the real registry in node ----------------------------------------------------------
_LOADER = """
const vm = require('vm');
const fs = require('fs');
// With `node -e` the script's own arguments start at argv[1], so the path is the last one.
const file = process.argv[process.argv.length - 1];
const sandbox = {
  window: { addEventListener() {} },
  document: { addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
  navigator: {},
  console,
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: file });
process.stdout.write(JSON.stringify({
  actions: sandbox.window.abpActions.list,
  groups: Object.keys(sandbox.window.abpActions.groups),
}));
"""


def _load(path: Path) -> dict:
    """Run the real registry file in node and hand back what it registered."""
    out = subprocess.run(["node", "-e", _LOADER, str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"node could not load {path.name}: {out.stderr}"
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def registry() -> dict:
    return _load(DASH / "action-registry.js")


@pytest.fixture(scope="module")
def engine() -> str:
    return (DASH / "shortcuts.js").read_text(encoding="utf-8")


# ---- the registry is one list, in both UIs --------------------------------------------------------
@needs_node
def test_both_uis_load_the_same_registry_and_engine():
    """The whole point of one registry: two UIs that cannot offer different things. The files must be
    byte-identical, and both pages must actually load them."""
    for name in SHARED_JS:
        dash = (DASH / name).read_bytes()
        desk = (DESK / name).read_bytes()
        assert dash == desk, f"{name} differs between the two UIs - copy the dashboard one over"
    for label, page in PAGES.items():
        html = page.read_text(encoding="utf-8")
        for name in SHARED_JS:
            assert re_search_script(html, name), f"{label}: {page.name} never loads {name}"
        # The palette and the help overlay need a button to reach them from the mouse too.
        assert 'id="btn-command-palette"' in html, f"{label}: nothing opens the command palette"
        assert 'id="btn-shortcuts"' in html, f"{label}: nothing opens the shortcut list"


def re_search_script(html: str, name: str) -> bool:
    return bool(re.search(r'<script src="(/static/)?' + re.escape(name) + r'"></script>', html))


@needs_node
def test_no_duplicate_ids_and_every_action_is_complete(registry):
    """An id is the only handle anything else has on an action (the desktop app's global shortcuts,
    the right-click menus, the saved bindings), so a duplicate or a blank one is a real bug."""
    seen = set()
    for action in registry["actions"]:
        assert action["id"], "an action with no id"
        assert action["id"] not in seen, f"{action['id']} is registered twice"
        seen.add(action["id"])
        assert action.get("label"), f"{action['id']} has no label for the palette"
        assert action.get("group") in registry["groups"], f"{action['id']} has no group"
        assert action.get("context"), f"{action['id']} has no context"
        assert callable(action.get("run")) is False, f"{action['id']}'s run came back from node as a value"


@needs_node
def test_the_defaults_conflict_only_where_they_really_would(registry):
    """Two actions may share a chord only when they can never both be live - the three Mod+Enter
    composers are the real case. Two screen-scoped actions on the same chord, or either one global,
    is a conflict: the second would silently never fire."""
    by_chord: dict[str, list[dict]] = {}
    for action in registry["actions"]:
        for chord in action.get("keys") or []:
            by_chord.setdefault(chord, []).append(action)
    for chord, actions in by_chord.items():
        for i in range(len(actions)):
            for j in range(i + 1, len(actions)):
                a, b = actions[i], actions[j]
                same_screen = a["context"] == b["context"]
                either_global = a["context"] == "global" or b["context"] == "global"
                assert not (same_screen or either_global), (
                    f"{chord} is claimed by {a['id']} ({a['context']}) and {b['id']} ({b['context']}), "
                    "and both could be live at once"
                )
    # Mod+Enter sending is the convention worth having in all three composers.
    senders = {a["id"] for a in by_chord.get("mod+enter", [])}
    assert senders == {"chat.send", "serverchat.send", "support.send"}


@needs_node
def test_no_default_chord_belongs_to_the_browser_or_the_os(registry):
    for action in registry["actions"]:
        for chord in action.get("keys") or []:
            for token in chord.split(" "):
                assert not shortcuts_api.is_reserved(token), f"{action['id']} defaults to {chord}, which the browser owns"


@needs_node
def test_no_default_chord_steals_a_lone_letter(registry):
    """A bare letter with no modifier and no chord prefix would fire while someone reads a page, and
    (on a Mac) would swallow a text-selection shortcut. Only the two obvious ones are allowed."""
    allowed_bare = {"?", "/"}
    for action in registry["actions"]:
        for chord in action.get("keys") or []:
            if " " in chord or "+" in chord:
                continue
            assert chord in allowed_bare, f"{action['id']} defaults to the bare key {chord!r}"


@needs_node
def test_every_action_id_the_ui_and_docs_use_exists(registry):
    """A typo in a right-click menu, a `run()` call or docs/shortcuts.md is not a runtime error, it
    is a menu item that does nothing - so it is checked here instead."""
    known = {a["id"] for a in registry["actions"]}
    referenced = set()
    for path in [DASH / "shortcuts.js", DASH / "action-registry.js"] + [DESK / n for n in SHARED_JS]:
        text = path.read_text(encoding="utf-8")
        referenced.update(_menu_ids(text))
        referenced.update(_run_ids(text))
    for label, page in PAGES.items():
        referenced.update(_run_ids(page.read_text(encoding="utf-8")))
    doc = (ROOT / "docs/shortcuts.md").read_text(encoding="utf-8")
    # The first column only: the table's last column holds the screen an action belongs to, and a
    # screen id is not an action id.
    documented = set(re.findall(r"^\| `([A-Za-z0-9.\-]+)` \|", doc, re.M))
    assert documented, "docs/shortcuts.md lost its action table"
    unknown = (referenced | documented) - known
    assert not unknown, f"action id(s) referenced but not in the registry: {sorted(unknown)}"
    # And the doc must cover the registry, not just part of it.
    missing = known - documented
    assert not missing, f"docs/shortcuts.md does not document: {sorted(missing)}"


def _menu_ids(text: str) -> set[str]:
    found: set[str] = set()
    for block in re.findall(r"\{ sel: '([^']+)', items: \[([^\]]*)\] \}", text):
        found.update(re.findall(r"'([a-z][a-z0-9.\-]+)'", block[1]))
    return found


def _run_ids(text: str) -> set[str]:
    found: set[str] = set()
    found.update(re.findall(r"run\(\s*'([a-z][a-z0-9.\-]*)'", text))
    found.update(re.findall(r"window\.abpActions\.run\(\s*'([a-z][a-z0-9.\-]*)'", text))
    found.update(re.findall(r"\.run\(\s*'([a-z][a-z0-9.\-]*)'", text))
    return found


# ---- the engine ----------------------------------------------------------------------------------
def test_the_engine_parses_in_both_uis():
    for path in [DASH / n for n in SHARED_JS] + [DESK / n for n in SHARED_JS]:
        r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{path.name}: {r.stderr}"


def test_the_engine_has_the_four_ways_in(engine):
    """The chord handler, the help overlay, the palette, the menus and the Tauri bridge are the
    feature; a file that grew a key handler and lost the others would pass every other test here."""
    for needle in ("tokenFor", "function resolve(", "openPalette", "openHelp", "setAttribute('role', 'menu')",
                   "abp://action", "window.__TAURI__", "ContextMenu", "ev.shiftKey",
                   "setAttribute('role', 'listbox')", "aria-activedescendant", "noteRecent"):
        assert needle in engine, f"the engine lost {needle}"


def test_the_engine_never_fires_a_shortcut_while_someone_is_typing(engine):
    """Only the send actions may run inside a text field; everything else is suppressed there, so
    typing "r" into a message never refreshes the page."""
    assert "if (typing && !action.whileTyping) return;" in engine
    assert "if (typingSomewhere(ev.target)) return;" in engine, "the context menu takes the browser's own menu away inside a text field"


def test_the_engine_does_not_replace_the_native_menu_in_a_text_field(engine):
    assert "if (typingSomewhere(ev.target)) return;" in engine


def test_the_engine_refuses_a_reserved_chord_while_recording(engine):
    assert "isReserved(chord)" in engine and "belongs to the browser or the OS" in engine


def test_the_reserved_lists_in_the_browser_and_on_the_server_agree():
    """Two copies of the same list is only safe if a test notices them drifting."""
    reserved_js = (DASH / "shortcuts.js").read_text(encoding="utf-8")
    block = re.search(r"const RESERVED = \[(.*?)\];", reserved_js, re.S)
    assert block, "shortcuts.js lost its RESERVED list"
    in_browser = set(re.findall(r"'([a-z0-9+`/?]+)'", block.group(1)))
    assert in_browser == set(shortcuts_api.RESERVED), (
        "the browser and the server disagree about which chords are off-limits: "
        f"{in_browser ^ set(shortcuts_api.RESERVED)}"
    )


# ---- the API -------------------------------------------------------------------------------------
def test_the_routes_need_a_token(client):
    assert client.get("/api/shortcuts").status_code == 401
    assert client.put("/api/shortcuts", json={"bindings": {"ui.palette": "mod+shift+y"}}).status_code == 401
    assert client.post("/api/shortcuts/check", json={"chord": "mod+shift+y"}).status_code == 401
    assert client.delete("/api/shortcuts").status_code == 401


def test_a_paired_device_may_see_the_bindings_but_not_change_them(client):
    from bot import db

    _key_id, key = db.create_api_key("my-phone", permission_tier="standard")
    phone = {"X-Dashboard-Token": key}
    assert client.get("/api/shortcuts", headers=phone).status_code == 200
    assert client.put("/api/shortcuts", headers=phone, json={"bindings": {"ui.palette": "mod+shift+y"}}).status_code == 401
    assert client.delete("/api/shortcuts", headers=phone).status_code == 401
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == {}, "the refused writes changed nothing"


def test_a_binding_survives_the_round_trip_and_a_restart(client, temp_db, monkeypatch):
    """The point of putting bindings on the server: the same keys in another browser, on the phone,
    and in the desktop app. That is only true if it is real config, read back after a fresh app."""
    sent = {"bindings": {"ui.palette": "mod+shift+y", "chat.send": "", "bot.toggle": "g b"}}
    assert client.put("/api/shortcuts", headers=H, json=sent).json() == {"bindings": sent["bindings"]}
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == sent["bindings"]

    # It really is in ABP's own config, under the other settings, comments and all.
    assert config.current["shortcuts"]["bindings"] == sent["bindings"]

    fresh = TestClient(build_app())
    assert fresh.get("/api/shortcuts", headers=H).json()["bindings"] == sent["bindings"]
    # An empty string is a real answer ("this one has no shortcut"), not "not mentioned".
    assert fresh.get("/api/shortcuts", headers=H).json()["bindings"]["chat.send"] == ""


def test_the_set_is_replaced_not_merged(client):
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y", "ui.theme": "mod+alt+j"}})
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.theme": "mod+alt+j"}})
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == {"ui.theme": "mod+alt+j"}


def test_delete_puts_everyone_back_on_the_defaults(client):
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y"}})
    assert client.delete("/api/shortcuts", headers=H).json() == {"bindings": {}}
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == {}


def test_a_chord_the_browser_owns_is_refused_and_nothing_changes(client):
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y"}})
    before = client.get("/api/shortcuts", headers=H).json()["bindings"]
    for chord in ("mod+t", "mod+w", "mod+shift+n", "f11", "alt+f4", "escape"):
        r = client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": chord}})
        assert r.status_code == 400, f"{chord} was accepted"
        assert "browser or the OS" in r.json()["detail"]
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == before


def test_two_actions_on_one_chord_is_refused_and_nothing_changes(client):
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y"}})
    before = client.get("/api/shortcuts", headers=H).json()["bindings"]
    clash = client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y", "ui.theme": "mod+shift+y"}})
    assert clash.status_code == 400
    assert "already bound to ui.palette" in clash.json()["detail"]
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == before


def test_junk_payloads_are_refused_by_shape(client):
    assert client.put("/api/shortcuts", headers=H, json={}).status_code == 400
    assert client.put("/api/shortcuts", headers=H, json={"bindings": ["mod+shift+y"]}).status_code == 400
    assert client.put("/api/shortcuts", headers=H, json={"bindings": {"UI Palette!": "mod+shift+y"}}).status_code == 400
    assert client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "ctrl+shift+~~"}}).status_code == 400
    assert client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "a b c d"}}).status_code == 400
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == {}


def test_canonical_forms_mean_the_same_thing_on_every_machine():
    assert shortcuts_api.normalize("Ctrl+K") == shortcuts_api.normalize("cmd+k") == "mod+k"
    assert shortcuts_api.normalize("Ctrl+Alt+Shift+C") == "mod+alt+shift+c"
    assert shortcuts_api.normalize("Option+X") == "alt+x"
    assert shortcuts_api.normalize("Esc") == "escape"
    assert shortcuts_api.normalize("  MOD+K  ") == "mod+k"
    assert shortcuts_api.normalize("g  vi") == "g vi", "a sequence's order is the chord"
    assert shortcuts_api.normalize("") == ""
    for bad in ("mod+", "hello world!", "mod+ctrl+!", "a b c d"):
        with pytest.raises(shortcuts_api.ShortcutError):
            shortcuts_api.normalize(bad)


def test_the_check_endpoint_answers_before_you_save(client):
    client.put("/api/shortcuts", headers=H, json={"bindings": {"ui.palette": "mod+shift+y"}})
    free = client.post("/api/shortcuts/check", headers=H, json={"chord": "mod+alt+g"})
    assert free.json() == {"ok": True, "chord": "mod+alt+g"}
    taken = client.post("/api/shortcuts/check", headers=H, json={"chord": "mod+shift+y"})
    assert taken.json()["ok"] is False and "ui.palette" in taken.json()["reason"]
    reserved = client.post("/api/shortcuts/check", headers=H, json={"chord": "mod+t"})
    assert reserved.json() == {"ok": False, "chord": "mod+t", "reason": "mod+t belongs to the browser or the OS"}
    junk = client.post("/api/shortcuts/check", headers=H, json={"chord": "ctrl+shift+~~"})
    assert junk.json()["ok"] is False
    # The page's in-progress set is what matters, not the saved one.
    pending = client.post("/api/shortcuts/check", headers=H,
                          json={"chord": "mod+alt+z", "bindings": {"ui.palette": "mod+alt+z"}})
    assert pending.json()["ok"] is False
    assert client.post("/api/shortcuts/check", headers=H, json={"chord": ""}).json()["ok"] is True


def test_a_hand_edited_config_that_makes_no_sense_falls_back_to_the_defaults(client, temp_db):
    """A typo in backends.yaml must not break the keyboard for everybody; the customiser overwrites it."""
    config.set_values({("shortcuts", "bindings"): {"ui.palette": "mod+t", "ui.theme": "mod+shift+y"}}, actor="test")
    assert shortcuts_api.stored() == {}
    assert client.get("/api/shortcuts", headers=H).json()["bindings"] == {}


def test_a_binding_only_matters_if_the_registry_has_the_action(client):
    """An id from a newer UI, or a stale one, is kept but simply unused: dropping it would make the
    round trip lie about what was saved, and the UI ignores ids it does not know."""
    r = client.put("/api/shortcuts", headers=H,
                   json={"bindings": {"ui.palette": "mod+shift+y", "action.from.the.future": "mod+alt+y"}})
    assert r.status_code == 200
    assert set(r.json()["bindings"]) == {"ui.palette", "action.from.the.future"}


def test_the_dashboard_serves_the_two_new_files(client):
    assert client.get("/static/action-registry.js").status_code == 200
    assert client.get("/static/shortcuts.js").status_code == 200


def test_the_documented_table_is_current():
    """docs/shortcuts.md's table is generated from the registry; a stale one is how the desktop app's
    agent ends up binding an id that no longer exists."""
    before = (ROOT / "docs/shortcuts.md").read_text(encoding="utf-8")
    r = subprocess.run(["node", "scripts/gen-shortcuts-table.js"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (ROOT / "docs/shortcuts.md").read_text(encoding="utf-8") == before, (
        "docs/shortcuts.md is out of date - run node scripts/gen-shortcuts-table.js"
    )