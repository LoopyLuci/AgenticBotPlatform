"""bot/studio: variants of ABP's UI with a live preview, edits from models applied exactly or refused, theme tokens,
apply and revert, the log and its datasets, the preview routes, and a screenshot diff in a real browser. The UI files
are a temporary copy of the real ones ($ABP_STUDIO_CODE_ROOT), so nothing here touches the live UI."""
from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from bot.studio import edits, generate, log, tokens, variants
from bot.studio.variants import StudioError

ROOT = Path(__file__).resolve().parent.parent
DASH = "bot/dashboard/static/dashboard.html"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    code = tmp_path / "code"
    for rel in (DASH, "bot/dashboard/static/ui-prelude.js", "bot/dashboard/static/vision-panel.js",
                "desktop-app/ui/vision-panel.js", "desktop-app/ui/index.html"):
        (code / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, code / rel)
    monkeypatch.setenv("ABP_STUDIO_CODE_ROOT", str(code))
    monkeypatch.setenv("ABP_STUDIO_DIR", str(tmp_path / "studio"))
    return code


# ---- edits ---------------------------------------------------------------------------------------------------------
def test_edit_blocks_apply_exactly_or_are_refused():
    answer = ("FILE: bot/dashboard/static/x.js\n<<<<<<< SEARCH\nconst a = 1;\n=======\nconst a = 2;\n>>>>>>> REPLACE\n"
              "NEW FILE: bot/dashboard/static/new.css\n```css\n.x{color:red}\n```\nEXPLANATION: changed a, added a style\n")
    p = edits.parse(answer)
    assert p.edits == [("bot/dashboard/static/x.js", "const a = 1;", "const a = 2;")]
    assert p.new_files == {"bot/dashboard/static/new.css": ".x{color:red}\n"} and p.explanation.startswith("changed a")
    assert edits.apply("x;\nconst a = 1;\ny;", "const a = 1;", "const a = 2;") == ("x;\nconst a = 2;\ny;", "")
    assert edits.apply("a;\na;", "a;", "b;")[1].startswith("its SEARCH text is in the file 2 times")
    assert edits.apply("a;", "zzz", "b")[1] == "its SEARCH text is not in the file"
    # trailing spaces and line endings may differ; only the matched lines change, every other line stays as it was
    cur = "keep   \r\nfoo();  \r\nbar();\r\ntail  "
    new, why = edits.apply(cur, "foo();\nbar();", "baz();")
    assert why == "" and new == "keep   \r\nbaz();\r\ntail  "


# ---- variants --------------------------------------------------------------------------------------------------------
def test_a_variant_overlays_the_ui_and_apply_and_revert_round_trip(ui):
    live_js = (ui / "bot/dashboard/static/vision-panel.js").read_text(encoding="utf-8")
    v = variants.create("Bigger buttons")
    vid = v["id"]
    assert variants.read(vid, "bot/dashboard/static/vision-panel.js") == live_js and variants.files(vid) == []
    for bad in ("../secrets.txt", "bot/vision/service.py", "bot/dashboard/static/.hidden.js", "C:/x/dashboard.html"):
        with pytest.raises(StudioError):
            variants.check_rel(bad)
    stamp = variants.version(vid)
    variants.write(vid, "bot/dashboard/static/vision-panel.js", live_js + "\n// studio\n")
    assert variants.version(vid) > stamp                                  # what the preview polls
    assert "// studio" in variants.diff(vid)["bot/dashboard/static/vision-panel.js"]
    assert (ui / "bot/dashboard/static/vision-panel.js").read_text(encoding="utf-8") == live_js   # live untouched
    assert generate.validate(vid) == []
    res = variants.apply(vid)
    # its twin in the desktop app was identical, so it changes too (tests require the two to stay the same)
    assert set(res["files"]) == {"bot/dashboard/static/vision-panel.js", "desktop-app/ui/vision-panel.js"}
    for rel in res["files"]:
        assert (ui / rel).read_text(encoding="utf-8").endswith("// studio\n")
    with pytest.raises(StudioError, match="applied"):
        variants.write(vid, "bot/dashboard/static/vision-panel.js", "x")
    variants.revert(res["backup"])
    for rel in res["files"]:
        assert (ui / rel).read_text(encoding="utf-8") == live_js
    # a broken file is caught before anything is applied
    w = variants.create("broken")["id"]
    variants.write(w, "bot/dashboard/static/vision-panel.js", "function (")
    assert generate.validate(w)
    variants.rate(w, 1, "it does not parse")
    variants.discard(w)
    kinds = [e["kind"] for e in log.read()]
    assert {"create", "edit", "apply", "revert", "rate", "discard"} <= set(kinds)


def test_theme_tokens_change_in_both_pages_and_both_dark_blocks(ui):
    vid = variants.create("teal")["id"]
    before = tokens.read(vid)
    assert before["light"]["--accent"] and before["dark"]["--accent"]
    tokens.set_tokens(vid, "dark", {"--accent": "#119988"})
    after = tokens.read(vid)
    assert after["dark"]["--accent"] == "#119988" and after["light"]["--accent"] == before["light"]["--accent"]
    html = variants.read(vid, DASH)
    assert html.count("--accent:#119988") == 2                             # the explicit and the OS-preference block
    assert "--accent:#119988" in variants.read(vid, "desktop-app/ui/index.html")
    for bad in ({"--accent": "url(http://x)"}, {"--nope-not-a-token": "#fff"}, {"accent": "#fff"}):
        with pytest.raises(StudioError):
            tokens.set_tokens(vid, "light", bad)


def test_datasets_hold_kept_and_dropped_proposals(ui):
    group = "g1"
    a = variants.create("a", group=group, origin="model")["id"]
    b = variants.create("b", group=group, origin="model")["id"]
    for vid, model in ((a, "p/m1"), (b, "p/m2")):
        log.event("generate", vid=vid, group=group, instruction="make the button blue", files=[DASH], model=model)
        variants.write(vid, "bot/dashboard/static/vision-panel.js", f"// {vid}\n", why="generate-edit", source=model)
    variants.rate(a, 5)
    variants.rate(b, 1)
    ds = log.datasets()
    sft = [json.loads(x) for x in Path(ds["sft.jsonl"]["path"]).read_text(encoding="utf-8").splitlines()]
    prefs = [json.loads(x) for x in Path(ds["prefs.jsonl"]["path"]).read_text(encoding="utf-8").splitlines()]
    assert [s["model"] for s in sft] == ["p/m1"] and "make the button blue" == sft[0]["instruction"]
    assert len(prefs) == 1 and prefs[0]["chosen_model"] == "p/m1" and prefs[0]["rejected_model"] == "p/m2"


def test_the_prompt_centres_long_files_on_the_focus(ui):
    p = generate.build_prompt("make the vision tabs bigger", [DASH], focus='id="vision"')
    assert 'id="vision"' in p and "lines not shown" in p and "FILE: bot/dashboard/static/dashboard.html" in p
    with pytest.raises(StudioError, match="provider/model"):
        import asyncio
        asyncio.run(generate.candidates(["no-slash"]))


# ---- the preview routes, and a screenshot diff in a real browser ----------------------------------------------------
@pytest.fixture
def server(ui, monkeypatch, temp_db):
    import uvicorn

    from bot.dashboard.server import build_app
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("DASHBOARD_PORT", str(port))
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


def test_the_preview_serves_the_variant_live(server):
    import httpx
    H = {"X-Dashboard-Token": "unused"}
    v = httpx.post(f"{server}/api/studio/variants", headers=H, json={"name": "preview"}).json()
    vid = v["id"]
    page = httpx.get(f"{server}/studio/v/{vid}/")
    assert page.status_code == 200 and f'src="/studio/v/{vid}/static/ui-prelude.js"' in page.text
    assert "/api/studio/variants/" in page.text and "nonce-" in page.headers["content-security-policy"]
    assert "window.__ABP_TOKEN__" in page.text                               # a local page load gets the token
    live = httpx.get(f"{server}/studio/v/{vid}/static/vision-panel.js").text
    httpx.put(f"{server}/api/studio/variants/{vid}/file", headers=H,
              json={"path": "bot/dashboard/static/vision-panel.js", "content": live + "\n// changed\n"})
    assert httpx.get(f"{server}/studio/v/{vid}/static/vision-panel.js").text.endswith("// changed\n")
    ver = httpx.get(f"{server}/api/studio/variants/{vid}/version", headers=H).json()["version"]
    assert ver > 0
    gallery = httpx.get(f"{server}/studio/v/{vid}/components?theme=dark")
    assert gallery.status_code == 200 and 'data-theme="dark"' in gallery.text and ".btn" in gallery.text
    assert httpx.get(f"{server}/studio/v/nope0000/").status_code == 404
    assert httpx.get(f"{server}/api/studio", headers={}).status_code in (401, 403)


def test_what_visibly_changed_is_seen_in_a_real_browser(server):
    from bot.studio import shots
    if not shots.browser_channel():
        pytest.skip("Microsoft Edge or Google Chrome is needed")
    pytest.importorskip("playwright")
    vid = variants.create("loud")["id"]
    tokens.set_tokens(vid, "light", {"--bg": "#ff00ff"})
    res = shots.shoot({"variant": f"{server}/studio/v/{vid}/components?theme=light",
                       "live": f"{server}/studio/v/{variants.create('plain')['id']}/components?theme=light"},
                      width=900, height=700, wait_ms=300)
    from bot.vision import service
    diff = service.compare(res["live"], res["variant"])
    assert diff["similarity"] < 0.95 and diff["changed_share"] > 0.3


def test_the_page_is_the_same_file_in_both_uis_and_both_pages_have_it():
    a = (ROOT / "bot/dashboard/static/studio-panel.js").read_text(encoding="utf-8")
    assert a == (ROOT / "desktop-app/ui/studio-panel.js").read_text(encoding="utf-8"), "copy studio-panel.js over"
    for page in (DASH, "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="studio"' in html and 'id="std-root"' in html and "studio-panel.js" in html and 'href="#studio"' in html
