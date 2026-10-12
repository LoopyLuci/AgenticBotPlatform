"""MH-D: a module's panel. One hand-written renderer; what a model proposes is only a declarative layout spec, checked
strictly, shown for a pick, and stored beside the module's files as a hand edit that a refresh keeps. The model is
faked (the fixtures come from test_module_assist); nothing generated is ever HTML or script."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from test_module_assist import fake, world  # noqa: F401 - fixtures

from bot.modules import assist, panel, registry
from bot.modules.client import ModuleError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "bot/dashboard/static/module-panel.js"
IDS = ["script.alpha", "script.beta"]

GOOD = {"version": 1, "title": "Fresh tools",
        "sections": [{"title": "Main", "note": "The ones used most.",
                      "operations": [{"id": "script.alpha", "label": "Count things", "output": "table"}]},
                     {"title": "Other", "operations": [{"id": "script.beta", "output": "text"}]}]}


def _gen(**kw):
    return asyncio.run(assist.generate("fresh", panel=True, **kw))


# ---- the spec is validated strictly ---------------------------------------------------------------------------------

def test_a_good_spec_passes_and_comes_back_clean():
    clean, problems = panel.validate_spec(GOOD, IDS)
    assert problems == [] and clean == GOOD


@pytest.mark.parametrize("bad, why", [
    ({"sections": [{"title": "x", "operations": [{"id": "script.new"}]}]}, "not an operation"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha", "output": "html"}]}]}, "output must be"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha", "output": "image"}]}]}, "output must be"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha", "argv": ["rm"]}]}]}, "not allowed"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha", "html": "<b>"}]}]}, "not allowed"),
    ({"sections": [{"title": "x", "html": "<script>alert(1)</script>", "operations": [{"id": "script.alpha"}]}]}, "not allowed"),
    ({"script": "alert(1)", "sections": [{"title": "x", "operations": [{"id": "script.alpha"}]}]}, "not allowed"),
    ({"sections": [{"title": "x" * 61, "operations": [{"id": "script.alpha"}]}]}, "longer than"),
    ({"title": "t" * 81, "sections": [{"title": "x", "operations": [{"id": "script.alpha"}]}]}, "longer than"),
    ({"sections": [{"title": "x", "note": "n" * 201, "operations": [{"id": "script.alpha"}]}]}, "longer than"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha", "label": "l" * 61}]}]}, "longer than"),
    ({"sections": [{"title": "x", "operations": [{"id": "script.alpha"}, {"id": "script.alpha"}]}]}, "appears twice"),
    ({"sections": [{"title": f"s{i}", "operations": [{"id": "script.alpha" if i == 0 else f"x.{i}"}]}
                   for i in range(13)]}, "more than 12"),
    ({"sections": [{"title": "x", "operations": [{"id": f"script.o{i}"} for i in range(31)]}]}, "more than 30"),
    ({"sections": []}, "non-empty"),
    ({"sections": "script.alpha"}, "non-empty"),
    ({"version": 2, "sections": [{"title": "x", "operations": [{"id": "script.alpha"}]}]}, "version"),
    ({"sections": [{"title": 5, "operations": [{"id": "script.alpha"}]}]}, "text expected"),
    ("a string", "object expected"),
])
def test_unknown_operations_kinds_keys_and_oversize_are_rejected(bad, why):
    clean, problems = panel.validate_spec(bad, IDS + [f"script.o{i}" for i in range(31)] + [f"x.{i}" for i in range(13)])
    assert clean is None and any(why in p for p in problems), problems


def test_the_total_number_of_operations_is_capped():
    ids = [f"g.o{i}" for i in range(61)]
    spec = {"sections": [{"title": f"s{i}", "operations": [{"id": x} for x in ids[i * 30:(i + 1) * 30]]} for i in range(3)]}
    clean, problems = panel.validate_spec(spec, ids)
    assert clean is None and any("in all" in p for p in problems)


# ---- the default layout --------------------------------------------------------------------------------------------

def test_the_default_layout_groups_by_the_id_prefix():
    lay = panel.default_layout(["service.start", "service.stop", "script.alpha", "plain"])
    assert [(s["title"], [o["id"] for o in s["operations"]]) for s in lay["sections"]] == [
        ("service", ["service.start", "service.stop"]), ("script", ["script.alpha"]), ("other", ["plain"])]
    assert panel.validate_spec(lay, ["service.start", "service.stop", "script.alpha", "plain"])[1] == []
    assert panel.default_layout([])["sections"] == []


def test_layout_without_a_spec_is_the_default(world):  # noqa: F811
    r = panel.layout("fresh")
    assert r["source"] == "default" and not r["has_spec"] and r["problems"] == []
    ids = {o["id"] for s in r["layout"]["sections"] for o in s["operations"]}
    assert {"script.alpha", "script.beta"} <= ids


def test_a_stored_spec_that_no_longer_validates_is_ignored_with_reasons(world):  # noqa: F811
    (world.overlay / panel.SPEC_FILE).write_text(json.dumps(
        {"sections": [{"title": "x", "operations": [{"id": "script.gone"}]}]}), encoding="utf-8")
    r = panel.layout("fresh")
    assert r["source"] == "default" and any("not an operation" in p for p in r["problems"])
    (world.overlay / panel.SPEC_FILE).write_text("{not json", encoding="utf-8")
    assert panel.layout("fresh")["problems"] and panel.layout("fresh")["source"] == "default"


# ---- proposing and keeping -----------------------------------------------------------------------------------------

def test_a_panel_proposal_is_shown_and_nothing_is_written_before_keep(world, fake):  # noqa: F811
    fake.answers = [{"panel": GOOD}]
    before = world.ops.read_bytes()
    b = _gen(n=1)
    assert b["kind"] == "panel" and b["proposals"][0]["ok"]
    assert b["proposals"][0]["proposal"]["panel"] == GOOD
    assert any(r["target"] == "panel" and "script.alpha (table)" in r["to"] for r in b["proposals"][0]["diff"])
    assert "Count things" not in fake.prompts[0] and "script.alpha" in fake.prompts[0] and "no HTML" in fake.prompts[0]
    assert not (world.overlay / panel.SPEC_FILE).exists() and world.ops.read_bytes() == before


@pytest.mark.parametrize("bad", [
    {"panel": {"sections": [{"title": "x", "operations": [{"id": "script.new"}]}]}},
    {"panel": {"sections": [{"title": "x", "operations": [{"id": "script.alpha", "output": "html"}]}]}},
    {"panel": {"sections": [{"title": "x" * 99, "operations": [{"id": "script.alpha"}]}]}},
    {"panel": {"sections": [{"title": "x", "operations": [{"id": "script.alpha"}]}]}, "commands": ["rm -rf /"]},
])
def test_an_invalid_panel_proposal_is_rejected_and_cannot_be_kept(world, fake, bad):  # noqa: F811
    fake.answers = [bad]
    b = _gen(n=1)
    p = b["proposals"][0]
    assert not p["ok"] and p["problems"]
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], p["n"])
    assert e.value.code == "rejected" and not (world.overlay / panel.SPEC_FILE).exists()


def test_keep_writes_the_spec_as_a_hand_edit_that_a_refresh_keeps(world, fake):  # noqa: F811
    from bot.modules import refresh
    fake.answers = [{"panel": GOOD}]
    b = _gen(n=1)
    ops_before = world.ops.read_bytes()
    r = assist.keep("fresh", b["batch"], 1)
    assert r["wrote"] == [panel.SPEC_FILE] and world.ops.read_bytes() == ops_before      # abp-ops.toml untouched
    f = world.overlay / panel.SPEC_FILE
    assert json.loads(f.read_text(encoding="utf-8")) == GOOD
    lay = panel.layout("fresh")
    assert lay["source"] == "spec" and lay["layout"] == GOOD and lay["problems"] == []
    # upstream moves; a refresh (apply) leaves the spec alone
    (world.maint / "gamma.py").write_text("print(1)\n", encoding="utf-8")
    world.push("add gamma")
    refresh.refresh("fresh", apply=True)
    assert json.loads(f.read_text(encoding="utf-8")) == GOOD
    lay = panel.layout("fresh")
    assert lay["source"] == "spec"                    # still valid: its operations all still exist


def test_a_combined_proposal_writes_wording_and_layout(world, fake):  # noqa: F811
    prop = {"description": "A small tool.", "operations": [{"id": "script.alpha", "summary": "Count n things"}], "panel": GOOD}
    fake.answers = [prop]
    b = asyncio.run(assist.generate("fresh", n=1))
    assert b["proposals"][0]["ok"], b["proposals"][0]["problems"]
    r = assist.keep("fresh", b["batch"], 1)
    assert "abp-ops.toml" in r["wrote"] and panel.SPEC_FILE in r["wrote"]


def test_keep_of_a_stale_panel_proposal_is_refused(world, fake):  # noqa: F811
    from abp_modkit import spec as sp
    fake.answers = [{"panel": GOOD}]
    b = _gen(n=1)
    spec = sp.load(world.ops)
    spec.op("script.beta").summary = "edited meanwhile"
    world.ops.write_text(sp.dump(spec, header=world.ops.read_text(encoding="utf-8").splitlines()[0]), encoding="utf-8")
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], 1)
    assert e.value.code == "stale" and not (world.overlay / panel.SPEC_FILE).exists()


def test_the_spec_travels_with_a_published_overlay(world, fake):  # noqa: F811
    from bot.modules import catalog
    fake.answers = [{"panel": GOOD}]
    assist.keep("fresh", _gen(n=1)["batch"], 1)
    assert panel.SPEC_FILE in catalog._overlay_files(registry.get("fresh"))


# ---- the API ------------------------------------------------------------------------------------------------------

def test_the_panel_route_needs_read_auth_only_and_assist_takes_panel():
    from fastapi import FastAPI
    from bot.dashboard import modules_api
    app = FastAPI()
    modules_api.register(app, read_auth=lambda: None, write_auth=lambda: None)
    got = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) or ()}
    assert ("GET", "/api/modules/{mid}/panel") in got and ("POST", "/api/modules/{mid}/assist") in got
    src = (ROOT / "bot/dashboard/modules_api.py").read_text(encoding="utf-8")
    assert re.search(r'@app\.get\("/api/modules/\{mid\}/panel", dependencies=read\)', src)
    assert "panel=body.get(\"panel\") is True" in src


# ---- the renderer: one hand-written file, identical in both UIs ---------------------------------------------------

def test_the_renderer_is_identical_in_both_uis_and_both_pages_load_it():
    assert STATIC.read_bytes() == (ROOT / "desktop-app/ui/module-panel.js").read_bytes()
    assert (ROOT / "bot/dashboard/static/modules-panel.js").read_bytes() == (ROOT / "desktop-app/ui/modules-panel.js").read_bytes()
    for p in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / p).read_text(encoding="utf-8")
        assert re.search(r'<script src="(/static/)?module-panel\.js"></script>', text)
        assert text.index("module-panel.js") < text.index("modules-panel.js")


def test_everything_in_the_renderer_goes_through_the_escape_helper():
    js = STATIC.read_text(encoding="utf-8")
    assert "const E = (s) => (typeof esc === 'function' ? esc(s)" in js
    assert "innerHTML" in js and "eval(" not in js and "new Function" not in js and "document.write" not in js
    assert "http://" not in js and "https://" not in js and "javascript:" not in js
    bad = []
    for expr in re.findall(r"\$\{([^{}]*)\}", js):
        e = expr.strip()
        if re.match(r"^E\(", e) or re.fullmatch(r"\w+(Html|Attr)", e) or re.fullmatch(r"cellHtml\(x\[c\]\)", e):
            continue
        bad.append(e)
    assert not bad, f"interpolations that do not go through E(): {bad}"
    for v in ("o.summary", "o.id", "label || o.id", "s.title", "s.note", "d.to", "p.model", "lay.layout.title", "m.description"):
        assert f"E({v})" in js or f"E({v} " in js or f"E({v}||" in js, v


def test_a_destructive_operation_asks_for_confirmation_before_it_runs():
    js = STATIC.read_text(encoding="utf-8")
    run = js[js.index("data-run]"):]
    gate = run.index("o.destructive && !confirm(")
    post = run.index("post(base + '/call'")
    assert gate < post and "return;" in run[gate:post]
    assert "o.mutating && !o.destructive && !confirm(" in run
    # the operation API, with the operation id and the typed form values, and nothing else
    assert "{ operation: o.id, args }" in js
    assert "/api/modules/" in js and "/panel'" in js and "/assist" in js


def test_the_form_is_built_from_the_typed_inputs():
    js = STATIC.read_text(encoding="utf-8")
    for needle in ("s.enum", "s.type === 'boolean'", "s.type === 'integer'", "s.description", "required", "input_schema"):
        assert needle in js, needle
    assert "KINDS = ['table', 'text', 'json']" in js


def test_the_spec_schema_has_no_room_for_markup_or_commands():
    # the only strings a spec holds are titles, notes and labels (escaped when drawn); everything else is an id or an enum
    assert set(panel.OUTPUT_KINDS) == {"table", "text", "json"}
    src = Path(panel.__file__).read_text(encoding="utf-8")
    assert '{"version", "title", "sections"}' in src and '{"title", "note", "operations"}' in src
    assert '{"id", "label", "output"}' in src
