"""Phases 2-3 of the Hermes-swarm plan: the new /api/hermes/{id}/delegation
and /api/hermes/{id}/dispatch routes, and bot/hermes_config.py's
comment-preserving read/write of Hermes's own ~/.hermes/config.yaml.

Exercised against the real FastAPI app via TestClient (matching
test_schedule_routes.py's precedent) with router.ask() and the Hermes
config file itself faked — this is about BotServer's own routing/config
logic, not a live Hermes process or a real home-directory file.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances, hermes_config
from bot.backends.base import BackendResult
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_hermes_instance(**overrides):
    kwargs = dict(
        name="hermes-worker", platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )
    kwargs.update(overrides)
    return bot_instances.create_instance(**kwargs)


def _create_cli_instance():
    return bot_instances.create_instance(
        name="cli-bot", platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


# --------------------------------------------------------------- hermes_config


def test_read_delegation_config_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")
    assert hermes_config.read_delegation_config() == {}


def test_set_delegation_config_creates_file_and_returns_delegation(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    result = hermes_config.set_delegation_config(provider="openrouter", model="some/model:free", max_concurrent_children=4)

    assert result == {"provider": "openrouter", "model": "some/model:free", "max_concurrent_children": 4}
    assert path.is_file()
    assert hermes_config.read_delegation_config() == result


def test_set_delegation_config_preserves_unrelated_keys_and_comments(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# a real user comment that must survive\n"
        "model: claude-opus-4.8\n"
        "delegation:\n"
        "  max_spawn_depth: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    hermes_config.set_delegation_config(provider="openrouter")

    text = path.read_text(encoding="utf-8")
    assert "a real user comment that must survive" in text
    assert "model: claude-opus-4.8" in text
    delegation = hermes_config.read_delegation_config()
    assert delegation["provider"] == "openrouter"
    assert delegation["max_spawn_depth"] == 2  # untouched key survives the merge


def test_set_delegation_config_no_op_with_no_changes(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", path)

    result = hermes_config.set_delegation_config()

    assert result == {}
    assert not path.exists()  # never created a file for a no-op call


def test_register_botserver_mcp_server_preserves_existing_mcp_servers_and_comments(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# a real user comment that must survive\n"
        "mcp_servers:\n"
        "  agentic_toolkit:\n"
        "    command: some-other-python\n"
        "    args: [\"-m\", \"agentic_mcp_toolkit.server\"]\n",
        encoding="utf-8",
    )

    result = hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path), dashboard_token="tok-123")

    assert result["name"] == "botserver"
    text = path.read_text(encoding="utf-8")
    assert "a real user comment that must survive" in text
    assert "agentic_toolkit" in text  # untouched sibling entry survives
    assert "some-other-python" in text
    assert "botserver" in text
    assert "bot.mcp_server" in text


def test_register_botserver_mcp_server_is_idempotent(tmp_path, monkeypatch, temp_db):
    path = tmp_path / "config.yaml"

    first = hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path))
    second = hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path))

    assert first["name"] == second["name"] == "botserver"
    text = path.read_text(encoding="utf-8")
    assert text.count("botserver:") == 1  # overwritten in place, not duplicated


# --------------------------------------------------------------------- routes


def test_delegation_route_rejects_non_hermes_gateway_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_cli_instance()

    resp = client.post(f"/api/hermes/{instance_id}/delegation", headers=_headers(), json={"provider": "openrouter"})

    assert resp.status_code == 400


def test_delegation_route_sets_and_reads_back(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"provider": "openrouter", "model": "meta-llama/llama-3.1-8b-instruct:free"},
    )
    assert resp.status_code == 200
    assert resp.json()["delegation"]["provider"] == "openrouter"

    resp = client.get(f"/api/hermes/{instance_id}/delegation", headers=_headers())
    assert resp.json()["delegation"]["model"] == "meta-llama/llama-3.1-8b-instruct:free"


def test_delegation_route_refuses_auto_approve_without_confirm(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"subagent_auto_approve": True},
    )

    assert resp.status_code == 400
    assert hermes_config.read_delegation_config() == {}


def test_delegation_route_allows_auto_approve_with_confirm(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    resp = client.post(
        f"/api/hermes/{instance_id}/delegation", headers=_headers(),
        json={"subagent_auto_approve": True, "confirm": True},
    )

    assert resp.status_code == 200
    assert resp.json()["delegation"]["subagent_auto_approve"] is True


def test_dispatch_route_requires_goal(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()

    resp = client.post(f"/api/hermes/{instance_id}/dispatch", headers=_headers(), json={})

    assert resp.status_code == 400


def test_dispatch_route_auto_picks_free_model_and_dispatches(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    async def fake_pricing(iid, refresh=False):
        return (
            {
                "openrouter": [
                    {"id": "paid-model", "free": False, "input": "$1", "output": "$2"},
                    {"id": "free-model", "free": True, "input": None, "output": None},
                ],
            },
            "live",
        )

    from bot import models as models_module

    monkeypatch.setattr(models_module, "hermes_models_with_pricing", fake_pricing)

    sent_prompts = []

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        sent_prompts.append(prompt)
        return BackendResult(text="all subtasks done", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    resp = client.post(f"/api/hermes/{instance_id}/dispatch", headers=_headers(), json={"goal": "build the thing"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "all subtasks done"
    assert body["worker_provider"] == "openrouter"
    assert body["worker_model"] == "free-model"
    assert "build the thing" in sent_prompts[0]
    assert "delegate_task" in sent_prompts[0]
    # The auto-picked free model was actually written as the delegation default.
    assert hermes_config.read_delegation_config()["model"] == "free-model"


def test_dispatch_route_respects_explicit_worker_model(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance()
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", tmp_path / "config.yaml")

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        return BackendResult(text="ok", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    resp = client.post(
        f"/api/hermes/{instance_id}/dispatch", headers=_headers(),
        json={"goal": "do it", "worker_provider": "ollama", "worker_model": "llama3:free"},
    )

    assert resp.status_code == 200
    assert resp.json()["worker_provider"] == "ollama"
    assert resp.json()["worker_model"] == "llama3:free"
    assert hermes_config.read_delegation_config()["provider"] == "ollama"


# -------------------------------------------------- backend-eviction wiring
# Found via live testing against a real Hermes gateway: editing a
# hermes_gateway instance's model orphaned its old Backend object (still
# holding a real spawned `hermes serve` subprocess) forever under an
# unreachable old cache key. These confirm the dashboard's update/delete
# routes actually call the fix (Router.evict_backend), not just that the
# method exists in isolation (see test_hermes_isolation.py for that).


def test_updating_model_evicts_the_old_cached_backend(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(model="old-model")

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.put(f"/api/bots/{instance_id}", headers=_headers(), json={"model": "new-model"})

    assert resp.status_code == 200
    assert evicted == [("hermes_gateway", "old-model", None)]


def test_updating_unrelated_field_does_not_evict(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(model="same-model")

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.put(f"/api/bots/{instance_id}", headers=_headers(), json={"custom_instructions": "be nice"})

    assert resp.status_code == 200
    assert evicted == []


def test_deleting_hermes_gateway_instance_evicts_its_backend(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(model="worker-model", hermes_home="/tmp/worker-home")

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.delete(f"/api/bots/{instance_id}", headers=_headers())

    assert resp.status_code == 200
    assert evicted == [("hermes_gateway", "worker-model", "/tmp/worker-home")]


# ------------------------------------------------ Hermes-organizes-swarms too
# The reverse direction: giving a Hermes agent itself the same
# cross-instance organizing ability Claude has via this MCP server, by
# registering bot-server's own MCP server into the Hermes instance's own
# config.yaml mcp_servers section.


def test_enable_swarm_tools_registers_mcp_server_and_evicts_backend(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(hermes_home=str(tmp_path / "home"))

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.post(f"/api/hermes/{instance_id}/enable-swarm-tools", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["registration"]["name"] == "botserver"
    assert evicted == [("hermes_gateway", None, str(tmp_path / "home"))]

    delegation = hermes_config.read_delegation_config(str(tmp_path / "home"))  # sanity: file is real YAML
    config_path = tmp_path / "home" / "config.yaml"
    assert config_path.is_file()
    text = config_path.read_text(encoding="utf-8")
    assert "mcp_servers" in text
    assert "botserver" in text
    assert "bot.mcp_server" in text


def test_enable_swarm_tools_rejects_non_hermes_instance(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_cli_instance()  # backend="cli" (Claude Code), not a Hermes backend at all

    resp = client.post(f"/api/hermes/{instance_id}/enable-swarm-tools", headers=_headers())

    assert resp.status_code == 400


def test_enable_swarm_tools_works_for_hermes_cli_with_no_eviction(temp_db, monkeypatch, tmp_path):
    # hermes_cli has no persistent gateway process to evict — registering
    # the MCP server alone is enough, since it spawns a fresh `hermes -z`
    # per call that re-reads config.yaml every time.
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(backend="hermes_cli", hermes_home=str(tmp_path / "home"))

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.post(f"/api/hermes/{instance_id}/enable-swarm-tools", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["registration"]["name"] == "botserver"
    assert "fresh gateway spawn" not in body["note"]
    assert evicted == []  # no eviction needed for hermes_cli
    assert hermes_config.is_botserver_mcp_registered(str(tmp_path / "home")) is True


def test_disable_swarm_tools_works_for_hermes_cli_with_no_eviction(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(backend="hermes_cli", hermes_home=str(tmp_path / "home"))
    hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path / "home"))

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    resp = client.post(f"/api/hermes/{instance_id}/disable-swarm-tools", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert evicted == []


# --------------------------------------------------- swarm-tools status panel


def test_is_botserver_mcp_registered_false_when_no_file(tmp_path, monkeypatch):
    assert hermes_config.is_botserver_mcp_registered(str(tmp_path / "nope")) is False


def test_is_botserver_mcp_registered_true_after_registering(tmp_path, monkeypatch, temp_db):
    home = str(tmp_path)
    assert hermes_config.is_botserver_mcp_registered(home) is False

    hermes_config.register_botserver_mcp_server(hermes_home=home)

    assert hermes_config.is_botserver_mcp_registered(home) is True


def test_unregister_botserver_mcp_server_removes_entry(tmp_path, monkeypatch, temp_db):
    home = str(tmp_path)
    hermes_config.register_botserver_mcp_server(hermes_home=home)
    assert hermes_config.is_botserver_mcp_registered(home) is True

    removed = hermes_config.unregister_botserver_mcp_server(hermes_home=home)

    assert removed is True
    assert hermes_config.is_botserver_mcp_registered(home) is False


def test_unregister_botserver_mcp_server_no_op_when_absent(tmp_path, monkeypatch, temp_db):
    home = str(tmp_path)
    removed = hermes_config.unregister_botserver_mcp_server(hermes_home=home)
    assert removed is False


def test_unregister_preserves_sibling_mcp_servers_and_comments(tmp_path, monkeypatch, temp_db):
    home = str(tmp_path)
    path = tmp_path / "config.yaml"
    hermes_config.register_botserver_mcp_server(hermes_home=home)
    # Hand-add a sibling entry + comment, simulating a real user's config.
    text = path.read_text(encoding="utf-8")
    path.write_text("# a real user comment\n" + text.replace(
        "mcp_servers:\n", "mcp_servers:\n  agentic_toolkit:\n    command: some-python\n    args: [\"-m\", \"x\"]\n",
    ), encoding="utf-8")

    hermes_config.unregister_botserver_mcp_server(hermes_home=home)

    final_text = path.read_text(encoding="utf-8")
    assert "a real user comment" in final_text
    assert "agentic_toolkit" in final_text
    assert "some-python" in final_text
    assert "botserver" not in final_text


def test_disable_route_evicts_only_when_something_was_removed(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    instance_id = _create_hermes_instance(hermes_home=str(tmp_path / "home"))

    from bot.router import router

    evicted = []

    async def fake_evict(name, model_override=None, hermes_home=None):
        evicted.append((name, model_override, hermes_home))

    monkeypatch.setattr(router, "evict_backend", fake_evict)

    # Nothing registered yet -> no-op, no eviction.
    resp = client.post(f"/api/hermes/{instance_id}/disable-swarm-tools", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["removed"] is False
    assert evicted == []

    hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path / "home"))
    resp = client.post(f"/api/hermes/{instance_id}/disable-swarm-tools", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert evicted == [("hermes_gateway", None, str(tmp_path / "home"))]


def test_swarm_tools_status_lists_both_hermes_backends_with_flag(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    enabled_id = _create_hermes_instance(name="enabled-worker", hermes_home=str(tmp_path / "a"))
    disabled_id = _create_hermes_instance(name="disabled-worker", hermes_home=str(tmp_path / "b"))
    cli_backed_id = _create_hermes_instance(
        name="cli-backed-worker", backend="hermes_cli", hermes_home=str(tmp_path / "c"),
    )
    _create_cli_instance()  # backend="cli" (Claude Code) — not a Hermes backend, must be excluded

    hermes_config.register_botserver_mcp_server(hermes_home=str(tmp_path / "a"))

    resp = client.get("/api/hermes/swarm-tools-status", headers=_headers())

    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()["instances"]}
    assert set(rows.keys()) == {enabled_id, disabled_id, cli_backed_id}
    assert rows[enabled_id]["swarm_tools_enabled"] is True
    assert rows[disabled_id]["swarm_tools_enabled"] is False
    assert rows[enabled_id]["name"] == "enabled-worker"
    assert rows[enabled_id]["backend"] == "hermes_gateway"
    assert rows[cli_backed_id]["backend"] == "hermes_cli"
