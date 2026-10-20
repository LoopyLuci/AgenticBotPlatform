"""ABP's capability index (PLAN-local-chat.md P5): generated, complete (every route, page, module, action and settings
section is in it), searchable by people (API) and by the agent (the read-only abp_capabilities tool), which answers
"how do I X in ABP" with the right place."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot import capabilities

ROOT = Path(__file__).resolve().parent.parent
INDEX = json.loads((ROOT / "docs/capabilities.json").read_text(encoding="utf-8"))
IDS = {e["id"] for e in INDEX["entries"]}


def test_the_generated_index_is_current():
    r = subprocess.run([sys.executable, "scripts/gen_capabilities.py", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr + "\nrun: python scripts/gen_capabilities.py"


def test_every_route_is_in_the_index():
    spec = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))
    missing = [f"{m.upper()} {p}" for p, ops in spec["paths"].items() for m in ops
               if m in ("get", "post", "put", "patch", "delete") and f"route:{m.upper()} {p}" not in IDS]
    assert not missing and len(spec["paths"]) > 800, missing[:10]


@pytest.mark.parametrize("page", ["desktop-app/ui/index.html", "bot/dashboard/static/dashboard.html"])
def test_every_page_is_in_the_index(page):
    html = (ROOT / page).read_text(encoding="utf-8")
    nav = html[html.index('<nav class="sidenav"'):html.index("</nav>", html.index('<nav class="sidenav"'))]
    missing = [p for p in re.findall(r'<a href="#([\w-]+)"', nav) if f"page:{p}" not in IDS]
    assert not missing, missing
    assert {"page:local-chat", "page:overview", "page:settings"} <= IDS


def test_every_action_module_and_settings_section_is_in_the_index():
    reg = (ROOT / "desktop-app/ui/action-registry.js").read_text(encoding="utf-8")
    actions = set(re.findall(r"\{ id: '([^']+)', label:", reg))
    assert actions and not {f"action:{a}" for a in actions} - IDS
    assert "action:nav.local-chat" in IDS and "action:nav.settings" in IDS
    from bot.modules import registry

    assert {f"module:{d['id'] if 'id' in d else d['module']['id']}" for d in registry.BUILTIN} <= IDS
    hub = (ROOT / "desktop-app/ui/settings-hub.js").read_text(encoding="utf-8")
    sections = re.findall(r"\{ id: '([\w-]+)', title:", hub)
    assert len(sections) == 12 and not {f"setting:{s}" for s in sections} - IDS


@pytest.mark.parametrize("question,where", [
    ("how do I add a bot", "#bots"),
    ("pair my phone with ABP", "#mobile"),
    ("change the theme", "settings/appearance"),
    ("approve the push queue", "settings/factory"),
    ("chat with a local model", "#local-chat"),
    ("run the backend setup wizard again", "settings/backends"),
])
def test_the_agent_answers_how_do_i_x_from_the_index(question, where):
    from bot.agent_runtime import toolspec
    from bot.capabilities import tool

    assert toolspec.spec_for("abp_capabilities").read_only and "abp_capabilities" in toolspec.registered_names()
    rows = json.loads(asyncio.run(tool._search({"query": question, "limit": 5})))
    assert where in [r["where"] for r in rows[:3]], (question, rows[:3])


def test_live_entries_include_the_agents_tools_and_local_chat_commands():
    live = {e["id"] for e in capabilities.live_entries()}
    assert {"tool:read_file", "tool:abp_capabilities", "command:/new"} <= live


def test_offline_search_leaves_out_what_needs_the_internet_or_a_cloud_model():
    rows = capabilities.search("tailscale funnel", limit=50)
    assert rows and any(r["network"] for r in rows)
    assert not any(r["network"] or r["cloud"] for r in capabilities.search("tailscale funnel", limit=50, offline=True))


def test_the_api(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "test-token"}
    assert c.get("/api/capabilities?q=bots").status_code in (401, 403)
    d = c.get("/api/capabilities?q=pair+phone&kind=page", headers=h).json()
    assert d["results"][0]["where"] == "#mobile" and {r["kind"] for r in d["results"]} == {"page"}
    st = c.get("/api/capabilities/stats", headers=h).json()
    assert st["by_kind"]["route"] > 800 and st["by_kind"]["tool"] > 10


def test_the_palette_and_settings_search_use_it():
    for f in ("desktop-app/ui/local-chat.js", "desktop-app/ui/settings-hub.js"):
        assert "/api/capabilities?" in (ROOT / f).read_text(encoding="utf-8"), f
