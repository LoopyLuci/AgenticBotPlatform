"""The ABP Agents page: every setting described once, validated, saved without losing the file's comments."""
from __future__ import annotations

import pathlib
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from bot.agent_runtime import settings_schema as schema
from bot.config import config
from bot.dashboard.server import build_app

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config" / "backends.yaml"
TOKEN = {"X-Dashboard-Token": "unused-dashboard-token"}
ROOTS = ("native_agent", "swarm_budget", "swarm_observability", "agent_control")
# Settings that are edited in the YAML on purpose (see schema.YAML_ONLY); matched as path prefixes.
YAML_ONLY_PREFIXES = (
    "native_agent.models.limits", "native_agent.models.overrides", "native_agent.context_windows",
    "native_agent.code_intel.lsp.servers", "native_agent.code_intel.formatters", "native_agent.mcp_trust",
    "native_agent.sandbox.docker",
)


def _leaves(node, prefix=""):
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            yield from _leaves(value, path)
        else:
            yield path


def test_every_setting_in_the_shipped_config_has_an_entry_on_the_page():
    cfg = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    missing = []
    for root in ROOTS:
        for leaf in _leaves(cfg.get(root) or {}, root):
            if leaf in schema.BY_ID or leaf.startswith(YAML_ONLY_PREFIXES):
                continue
            missing.append(leaf)
    assert not missing, f"settings with no entry in bot/agent_runtime/settings_schema.py (add one, or list it as YAML-only): {missing}"


def test_the_defaults_shown_match_the_shipped_config():
    cfg = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    for f in schema.FIELDS:
        node = cfg
        for part in schema._path(f):
            node = node.get(part) if isinstance(node, dict) else None
        if node is None or f["type"] == "rules":
            continue
        assert node == f["default"], f"{f['id']}: the page says {f['default']!r} but the shipped config has {node!r}"


def test_every_field_is_well_formed_and_belongs_to_a_tab():
    tabs = {t["id"] for t in schema.TABS}
    assert len(schema.BY_ID) == len(schema.FIELDS)  # ids are unique
    for f in schema.FIELDS:
        assert f["tab"] in tabs and f["section"] and f["label"] and f["help"], f["id"]
        if f["type"] == "enum":
            assert f["choices"] and f["default"] in [c[0] for c in f["choices"]], f["id"]
        if f["type"] in ("int", "float") and f["min"] is not None and f["max"] is not None:
            assert f["min"] <= f["default"] <= f["max"], f["id"]


# ------------------------------------------------------------------ validation
def _one(fid, value):
    return schema.validate({fid: value})


def test_valid_values_become_config_edits_by_path():
    edits, errors = schema.validate({"native_agent.limits.max_iterations": 12, "swarm_budget.enabled": False,
                                     "native_agent.web.allow_hosts": ["a.example", " ", "b.example"]})
    assert errors == {}
    assert edits[("native_agent", "limits", "max_iterations")] == 12
    assert edits[("swarm_budget", "enabled")] is False
    assert edits[("native_agent", "web", "allow_hosts")] == ["a.example", "b.example"]


@pytest.mark.parametrize("fid, value, fragment", [
    ("native_agent.limits.max_iterations", 0, "at least 1"),
    ("native_agent.limits.max_iterations", 5000, "at most 1000"),
    ("native_agent.limits.max_iterations", 2.5, "whole number"),
    ("native_agent.limits.max_iterations", "ten", "number"),
    ("native_agent.limits.max_iterations", True, "number"),
    ("native_agent.context.compact_at", 1.5, "at most"),
    ("native_agent.web.enabled", "yes", "on or off"),
    ("native_agent.permissions.mode", "wild", "one of"),
    ("native_agent.sandbox.backend", "vm", "one of"),
    ("native_agent.prompt.extra", 12, "text"),
    ("native_agent.web.allow_hosts", [1, 2], "list of text"),
    ("swarm_budget.max_estimated_usd", -1, "at least"),
    ("native_agent.no_such_setting", 1, "unknown"),
    ("native_agent.limits.max_seconds", None, "required"),
])
def test_bad_values_are_refused_with_a_reason(fid, value, fragment):
    edits, errors = _one(fid, value)
    assert edits == {} and fragment in errors[fid]


def test_nullable_settings_accept_blank():
    edits, errors = _one("native_agent.mcp_sampling.model", "")
    assert errors == {} and edits[("native_agent", "mcp_sampling", "model")] is None


def test_permission_rules_are_written_as_lines_and_checked():
    edits, errors = _one("native_agent.permissions.rules", ["deny run_shell rm -rf*  # never", "allow read_file"])
    assert errors == {}
    assert edits[("native_agent", "permissions", "rules")] == [
        {"decision": "deny", "tool": "run_shell", "match": "rm -rf*", "note": "never"},
        {"decision": "allow", "tool": "read_file"},
    ]
    _, errors = _one("native_agent.permissions.rules", ["maybe run_shell"])
    assert "decision" in errors["native_agent.permissions.rules"]
    assert schema.lines_to_rules(schema.rules_to_lines(edits[("native_agent", "permissions", "rules")])) == edits[("native_agent", "permissions", "rules")]


# ------------------------------------------------------------------ saving
@pytest.fixture
def temp_config(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "backends.yaml"
    shutil.copy(SHIPPED, path)
    monkeypatch.setattr(config, "path", path)
    monkeypatch.setattr(config, "_data", dict(config._data))
    monkeypatch.setattr(config, "version", config.version)
    config.reload(actor="test")
    return path


def test_saving_keeps_the_files_comments_and_takes_effect_at_once(temp_config):
    before = temp_config.read_text(encoding="utf-8")
    comments = [ln for ln in before.splitlines() if ln.strip().startswith("#")]
    assert comments  # the shipped file is documented

    values, errors = schema.apply({"native_agent.limits.max_iterations": 7, "native_agent.web.enabled": True,
                                   "swarm_budget.max_children": 3})
    assert errors == {}
    assert values["native_agent.limits.max_iterations"] == 7
    assert config.current["native_agent"]["limits"]["max_iterations"] == 7
    assert config.current["swarm_budget"]["max_children"] == 3

    after = temp_config.read_text(encoding="utf-8")
    assert [ln for ln in after.splitlines() if ln.strip().startswith("#")] == comments
    assert yaml.safe_load(after)["native_agent"]["web"]["enabled"] is True


def test_nothing_is_written_when_any_change_is_invalid(temp_config):
    before = temp_config.read_bytes()
    values, errors = schema.apply({"native_agent.limits.max_iterations": 9, "native_agent.context.compact_at": 9})
    assert "native_agent.context.compact_at" in errors
    assert temp_config.read_bytes() == before


def test_reset_puts_settings_back_to_their_defaults(temp_config):
    schema.apply({"native_agent.limits.max_iterations": 7})
    values = schema.reset(["native_agent.limits.max_iterations"])
    assert values["native_agent.limits.max_iterations"] == 30
    assert config.current["native_agent"]["limits"]["max_iterations"] == 30


# ------------------------------------------------------------------ routes
@pytest.fixture
def client(monkeypatch, temp_config):
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    return TestClient(build_app())


def test_the_routes_describe_read_and_save_settings(client):
    described = client.get("/api/agent/config/schema", headers=TOKEN).json()
    assert {t["id"] for t in described["tabs"]} == {"runtime", "safety", "tools", "subagents", "models"}
    assert len(described["fields"]) == len(schema.FIELDS) and described["yaml_only"]

    got = client.get("/api/agent/config", headers=TOKEN).json()
    assert got["values"]["native_agent.limits.max_iterations"] == 30

    saved = client.post("/api/agent/config", json={"changes": {"native_agent.limits.max_iterations": 11}}, headers=TOKEN)
    assert saved.status_code == 200 and saved.json()["values"]["native_agent.limits.max_iterations"] == 11
    assert "native_agent.limits.max_iterations" in saved.json()["configured"]


def test_a_bad_save_is_a_422_with_per_setting_reasons_and_changes_nothing(client):
    bad = client.post("/api/agent/config", json={"changes": {"native_agent.limits.max_iterations": 0, "native_agent.web.enabled": True}}, headers=TOKEN)
    assert bad.status_code == 422
    assert "at least" in bad.json()["detail"]["errors"]["native_agent.limits.max_iterations"]
    assert client.get("/api/agent/config", headers=TOKEN).json()["values"]["native_agent.web.enabled"] is False
    assert client.post("/api/agent/config", json={"changes": {}}, headers=TOKEN).status_code == 400


def test_reset_route(client):
    client.post("/api/agent/config", json={"changes": {"native_agent.limits.max_iterations": 11}}, headers=TOKEN)
    reset = client.post("/api/agent/config/reset", json={"ids": ["native_agent.limits.max_iterations"]}, headers=TOKEN)
    assert reset.json()["values"]["native_agent.limits.max_iterations"] == 30


def test_the_overview_lists_agent_bots_with_their_effective_settings(client):
    from bot import db

    conn = db.get_conn()
    for name, backend in (("agent-bot", "native_agent"), ("cli-bot", "cli")):
        conn.execute("INSERT INTO bot_instances (name, platform, backend, credentials, enabled, created_at, updated_at) "
                     "VALUES (?, 'telegram', ?, '{}', 1, datetime('now'), datetime('now'))", (name, backend))
    conn.commit()
    body = client.get("/api/agent/overview", headers=TOKEN).json()
    assert [b["name"] for b in body["bots"]] == ["agent-bot"]  # a CLI bot does not run an ABP agent
    bot = body["bots"][0]
    assert bot["backend_label"] == "ABP Agent" and bot["permission_mode"] in ("default", "plan", "accept_edits", "bypass")
    assert bot["max_concurrent_children"] >= 1
    checks = {c["id"]: c for c in body["checks"]}
    assert checks["bot"]["ok"] is True
    assert {"provider", "bot", "permissions", "sandbox", "budget", "review"} <= set(checks)
    assert body["counts"]["tools"] > 10


def test_the_tool_inventory_lists_every_tool_with_how_it_is_approved(client):
    tools = client.get("/api/agent/tools", headers=TOKEN).json()["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["read_file"]["asks_first"] is False and by_name["read_file"]["read_only"] is True
    assert by_name["run_shell"]["asks_first"] is True and by_name["run_shell"]["permission"] == "execute"
    assert all(t["description"] for t in tools)


def test_the_agent_routes_need_the_token(client):
    for method, path in (("get", "/api/agent/config"), ("get", "/api/agent/config/schema"), ("get", "/api/agent/overview"),
                         ("get", "/api/agent/tools"), ("post", "/api/agent/config"), ("post", "/api/agent/config/reset")):
        response = getattr(client, method)(path)
        assert response.status_code in (401, 403, 503), (path, response.status_code)


# ------------------------------------------------------------------ per-bot permission mode
def _add_agent_bot(name="agent-bot"):
    from bot import db

    conn = db.get_conn()
    cur = conn.execute("INSERT INTO bot_instances (name, platform, backend, credentials, enabled, created_at, updated_at) "
                       "VALUES (?, 'telegram', 'native_agent', '{}', 1, datetime('now'), datetime('now'))", (name,))
    conn.commit()
    return cur.lastrowid


def test_a_bots_own_permission_mode_can_be_set_and_cleared_back_to_the_global_default(client):
    bot_id = _add_agent_bot()
    own = lambda: client.get("/api/agent/overview", headers=TOKEN).json()["bots"][0]["permission_mode_own"]  # noqa: E731
    assert own() is None
    assert client.put(f"/api/instances/{bot_id}/permissions", json={"mode": "plan"}, headers=TOKEN).status_code == 200
    assert own() == "plan"
    assert client.put(f"/api/instances/{bot_id}/permissions", json={"mode": ""}, headers=TOKEN).status_code == 200
    assert own() is None  # follows the global default again
    assert client.put(f"/api/instances/{bot_id}/permissions", json={"mode": "wild"}, headers=TOKEN).status_code == 400


def test_dangerous_settings_say_when_their_warning_applies():
    by = schema.BY_ID
    assert by["native_agent.permissions.allow_bypass"]["danger_value"] is True
    assert by["native_agent.sandbox.backend"]["danger_value"] == "local"
    assert by["native_agent.sandbox.env.mode"]["danger_value"] == "inherit"
    for f in schema.FIELDS:
        if f["danger"] and f["type"] == "enum":
            assert f["danger_value"] in [c[0] for c in f["choices"]], f["id"]


def test_a_bots_own_agent_settings_are_separate_from_what_it_inherits(client):
    bot_id = _add_agent_bot()
    assert client.post("/api/agent-settings", json={"instance_id": None, "max_concurrent_children": 9}, headers=TOKEN).status_code == 200
    resolved = client.get(f"/api/agent-settings?instance_id={bot_id}", headers=TOKEN).json()
    own = client.get(f"/api/agent-settings?instance_id={bot_id}&own=true", headers=TOKEN).json()
    assert resolved["max_concurrent_children"] == 9          # inherited from the process-wide default
    assert own["max_concurrent_children"] is None           # but not something this bot set itself
    assert set(own) == set(resolved)

    client.post("/api/agent-settings", json={"instance_id": bot_id, "max_concurrent_children": 3, "require_plan_approval": True}, headers=TOKEN)
    own = client.get(f"/api/agent-settings?instance_id={bot_id}&own=true", headers=TOKEN).json()
    assert own["max_concurrent_children"] == 3 and own["require_plan_approval"] is True
    assert own["worker_model"] is None
