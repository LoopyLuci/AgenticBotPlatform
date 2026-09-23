"""abp_cli - exercised against the real dashboard app (bot/dashboard/server.py's build_app())
over an in-process ASGITransport, same pattern tests/test_tui.py uses for the TUI's own
DashboardClient - real request/response handling, not a mock."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys

import httpx
import pytest

from abp_cli.__main__ import _dispatch, _parser
from bot import bot_instances
from bot.config import config
from bot.dashboard.server import build_app
from bot.dashboard_client import ApiError, DashboardClient


@pytest.fixture
def client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    # See tests/test_tui.py's dashboard_client fixture: config/backends.yaml is a
    # module-level singleton — redirect it to a throwaway copy so an agent-config test
    # never writes the real project file.
    shipped = config.path
    temp_path = tmp_path / "backends.yaml"
    shutil.copy(shipped, temp_path)
    monkeypatch.setattr(config, "path", temp_path)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    c = DashboardClient("http://testserver", "test-token", transport=transport)
    yield c
    asyncio.run(c.aclose())


def run(args_list, client):
    """Drives _dispatch through the same try/except _run() wraps it in — an already-open
    test client is reused (never a fresh connection/aclose per call), so this mirrors
    _run's error handling without duplicating its own connection setup."""
    args = _parser().parse_args(args_list)

    async def _wrapped():
        try:
            return await _dispatch(args, client)
        except ApiError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"couldn't reach the dashboard: {exc}", file=sys.stderr)
            return 1
    return asyncio.run(_wrapped()), args


def _create_instance(**overrides):
    creds = overrides.pop("credentials", None) or {"bot_token": "123456789:AAExampleTokenFromBotFather1234"}
    return bot_instances.create_instance(
        name=overrides.pop("name", "cli-bot"), platform=overrides.pop("platform", "telegram"),
        backend=overrides.pop("backend", "native_agent"), credentials=creds,
        allowed_user_ids=overrides.pop("allowed_user_ids", [111]), enabled=overrides.pop("enabled", False),
        **overrides,
    )


def test_bots_list_and_show(client, capsys):
    iid = _create_instance(name="alpha")
    code, _ = run(["--json", "bots", "list"], client)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert any(b["name"] == "alpha" for b in out)

    code, _ = run(["--json", "bots", "show", str(iid)], client)
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == iid


def test_bots_create_app_only(client, capsys):
    code, _ = run(
        ["--json", "bots", "create", "--name", "app-bot", "--platform", "app", "--backend", "native_agent"],
        client,
    )
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True and isinstance(created["id"], int)

    code, _ = run(["--json", "bots", "show", str(created["id"])], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["platform"] == "app"


def test_bots_create_rejects_bad_json_credentials(client, capsys):
    with pytest.raises(SystemExit) as exc_info:
        run(["bots", "create", "--name", "x", "--platform", "telegram", "--backend", "native_agent",
             "--credentials", "{not json"], client)
    assert exc_info.value.code == 2
    assert "must be valid JSON" in capsys.readouterr().err


def test_bots_edit_start_stop_enable_disable_delete(client, capsys):
    iid = _create_instance(name="bravo", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])
    code, _ = run(["bots", "edit", str(iid), "--name", "bravo-renamed"], client)
    assert code == 0
    capsys.readouterr()   # discard the edit result before checking the follow-up show
    code, _ = run(["--json", "bots", "show", str(iid)], client)
    assert json.loads(capsys.readouterr().out)["name"] == "bravo-renamed"

    for sub in ("enable", "start", "stop", "disable"):
        code, _ = run(["bots", sub, str(iid)], client)
        capsys.readouterr()
        assert code == 0

    code, _ = run(["bots", "delete", str(iid)], client)
    assert code == 0


def test_bots_edit_with_no_flags_is_a_usage_error(client):
    iid = _create_instance()
    code, _ = run(["bots", "edit", str(iid)], client)
    assert code == 2


def test_chat_sends_a_real_message_and_prints_the_reply(client, capsys, monkeypatch):
    from types import SimpleNamespace

    from bot.router import router

    async def fake_ask(text, **kw):
        return SimpleNamespace(text=f"echo: {text}")
    monkeypatch.setattr(router, "ask", fake_ask)

    iid = _create_instance(name="chatty", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])
    code, _ = run(["chat", str(iid), "hello", "there"], client)
    assert code == 0
    assert "echo: hello there" in capsys.readouterr().out


def test_agent_settings_get_and_set(client, capsys):
    iid = _create_instance(name="settings-bot", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "agent-settings", "get"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)   # doesn't raise — real defaults come back

    code, _ = run(["agent-settings", "set", "--instance", str(iid), "worker_effort=high"], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["--json", "agent-settings", "get", "--instance", str(iid), "--own"], client)
    assert code == 0
    own = json.loads(capsys.readouterr().out)
    assert own["worker_effort"] == "high"


def test_agent_config_schema_get_and_set(client, capsys):
    code, _ = run(["--json", "agent-config", "schema"], client)
    assert code == 0
    schema = json.loads(capsys.readouterr().out)
    assert "fields" in schema and any(f["id"] == "native_agent.sandbox.backend" for f in schema["fields"])

    code, _ = run(["agent-config", "set", "native_agent.web.enabled=true"], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["--json", "agent-config", "get"], client)
    assert code == 0
    values = json.loads(capsys.readouterr().out)["values"]
    assert values["native_agent.web.enabled"] is True


def test_agent_config_set_reports_a_validation_error(client, capsys):
    code, _ = run(["agent-config", "set", "native_agent.sandbox.backend=not-a-real-backend"], client)
    assert code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_providers_add_list_models_toggle_remove(client, capsys):
    code, _ = run(["providers", "add", "--name", "cli-test-provider", "--base-url", "http://127.0.0.1:11434/v1"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "providers", "list"], client)
    assert code == 0
    names = [p["name"] for p in json.loads(capsys.readouterr().out)]
    assert "cli-test-provider" in names

    code, _ = run(["providers", "remove", "cli-test-provider"], client)
    assert code == 0


def test_unreachable_host_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setenv("DASHBOARD_TOKEN", "whatever")
    args = _parser().parse_args(["--host", "127.0.0.1:1", "--token", "whatever", "bots", "list"])
    from abp_cli.__main__ import _run
    code = asyncio.run(_run(args))
    assert code == 1
    assert "couldn't reach" in capsys.readouterr().err.lower()


# ==================================================================================
# Phase 2: swarms, sessions, terminal, hooks, plugins, skills, mcp, security,
# snapshots, env, config, diagnostics, kanban (peers needs a real linked server,
# not exercised here beyond argument parsing).
# ==================================================================================

def test_swarms_full_lifecycle(client, capsys):
    iid = _create_instance(name="swarm-member", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "swarms", "create", "--name", "s1", "--strategy", "leader_vote",
                   "--config", json.dumps({"members": [iid], "leader": iid})], client)
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    swarm_id = created["id"]

    code, _ = run(["--json", "swarms", "list"], client)
    assert code == 0
    assert any(s["id"] == swarm_id for s in json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "swarms", "show", str(swarm_id)], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["name"] == "s1"

    for sub in ("disable", "enable"):
        code, _ = run(["swarms", sub, str(swarm_id)], client)
        capsys.readouterr()
        assert code == 0

    code, _ = run(["--json", "swarms", "runs"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)   # doesn't raise - real (empty) list

    code, _ = run(["swarms", "delete", str(swarm_id)], client)
    assert code == 0


def test_swarms_create_rejects_a_bad_config(client, capsys):
    code, _ = run(["swarms", "create", "--name", "bad", "--strategy", "leader_vote", "--config", "{}"], client)
    assert code == 1
    assert "references no bot instances" in capsys.readouterr().err


def test_sessions_list_and_new(client, capsys):
    iid = _create_instance(name="session-bot", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "sessions", "list", "--instance", str(iid)], client)
    assert code == 0
    json.loads(capsys.readouterr().out)   # a real (possibly legacy-bucket) list


def test_terminal_runs_a_real_slash_command(client, capsys):
    code, _ = run(["terminal", "/help"], client)
    assert code == 0
    assert capsys.readouterr().out.strip()


def test_terminal_rejects_non_slash_text(client, capsys):
    code, _ = run(["terminal", "hello"], client)
    assert code == 0
    assert "not a recognized command" in capsys.readouterr().out.lower()


def test_hooks_full_lifecycle(client, capsys):
    code, _ = run(["--json", "hooks", "add", "--event", "PreToolUse", "--command", "echo hi"], client)
    assert code == 0
    hook_id = json.loads(capsys.readouterr().out)["id"]

    code, _ = run(["--json", "hooks", "list"], client)
    assert code == 0
    assert any(h["id"] == hook_id for h in json.loads(capsys.readouterr().out))

    for sub in ("disable", "remove"):
        code, _ = run(["hooks", sub, str(hook_id)], client)
        capsys.readouterr()
        assert code == 0


def test_plugins_list_is_reachable(client, capsys):
    code, _ = run(["--json", "plugins", "list"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_plugins_create_and_remove(client, capsys):
    code, _ = run(["--json", "plugins", "create", "--name", "cli_test_plugin",
                   "--code", "def setup(api):\n    pass\n"], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["plugins", "remove", "cli_test_plugin"], client)
    assert code == 0


def test_skills_create_list_and_remove(client, capsys):
    iid = _create_instance(name="skill-bot", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["skills", "create", "--instance", str(iid), "--name", "greet",
                   "--description", "says hi", "--content", "Always greet warmly."], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "skills", "list", "--instance", str(iid)], client)
    assert code == 0
    assert any(s["name"] == "greet" for s in json.loads(capsys.readouterr().out))

    code, _ = run(["skills", "remove", "greet", "--instance", str(iid)], client)
    assert code == 0


def test_skills_packs_quarantine_and_drafts_are_reachable(client, capsys):
    for sub in ("packs", "quarantine", "drafts"):
        code, _ = run(["--json", "skills", sub], client)
        assert code == 0
        json.loads(capsys.readouterr().out)


def test_mcp_internal_list_is_reachable(client, capsys):
    code, _ = run(["--json", "mcp", "list"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_mcp_pins_is_reachable(client, capsys):
    code, _ = run(["--json", "mcp", "pins"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_mcp_external_add_list_and_remove(client, capsys):
    code, _ = run(["--json", "mcp", "external-add", "--name", "cli-test-mcp", "--transport", "stdio",
                   "--command", "python", "--args", json.dumps(["-m", "some_server"])], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "mcp", "external-list"], client)
    assert code == 0
    assert any(s["name"] == "cli-test-mcp" for s in json.loads(capsys.readouterr().out))

    code, _ = run(["mcp", "external-remove", "cli-test-mcp"], client)
    assert code == 0


def test_security_allowed_users_and_permissions(client, capsys):
    code, _ = run(["--json", "security", "allow-user", "555", "--name", "tester"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "security", "allowed-users"], client)
    assert code == 0
    assert any(str(u.get("telegram_id")) == "555" for u in json.loads(capsys.readouterr().out))

    code, _ = run(["security", "disallow-user", "555"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "security", "permissions"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_security_devices_and_mobile_keys(client, capsys):
    code, _ = run(["--json", "security", "devices"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)

    code, _ = run(["--json", "security", "create-mobile-key", "--label", "test-phone", "--tier", "standard"], client)
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["key"]   # the plaintext key, only ever returned once

    code, _ = run(["--json", "security", "mobile-keys"], client)
    assert code == 0
    assert any(k["label"] == "test-phone" for k in json.loads(capsys.readouterr().out))

    code, _ = run(["security", "revoke-mobile-key", str(created["id"])], client)
    assert code == 0


def test_snapshots_full_lifecycle(client, capsys):
    code, _ = run(["--json", "snapshots", "create", "--label", "cli-test"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "snapshots", "list"], client)
    assert code == 0
    snaps = json.loads(capsys.readouterr().out)
    assert snaps
    name = snaps[0]["name"]

    code, _ = run(["snapshots", "remove", name], client)
    assert code == 0


def test_env_status_is_reachable(client, capsys):
    code, _ = run(["--json", "env"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_config_get_and_reload(client, capsys):
    code, _ = run(["--json", "config", "get"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)

    code, _ = run(["config", "reload"], client)
    assert code == 0


def test_diagnostics_summary_and_crash_reports(client, capsys):
    code, _ = run(["--json", "diagnostics", "summary"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)

    code, _ = run(["--json", "diagnostics", "crash-reports"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_kanban_full_lifecycle(client, capsys):
    iid = _create_instance(name="kanban-bot", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "kanban", "add", "--instance", str(iid), "--text", "write tests"], client)
    assert code == 0
    card = json.loads(capsys.readouterr().out)["card"]

    code, _ = run(["--json", "kanban", "cards", "--instance", str(iid)], client)
    assert code == 0
    assert any(c["id"] == card["id"] for c in json.loads(capsys.readouterr().out))

    code, _ = run(["kanban", "move", str(card["id"]), "--instance", str(iid), "--column", "done"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["kanban", "remove", str(card["id"]), "--instance", str(iid)], client)
    assert code == 0


def test_peers_list_is_reachable(client, capsys):
    code, _ = run(["--json", "peers", "list"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


# ==================================================================================
# SSH Toolkit (github.com/LoopyLuci/SSH_Toolkit, vendored as a git submodule at
# vendor/ssh_toolkit) - real end-to-end calls into the real PowerShell tool, isolated
# from the real ~/.ssh/config via ABP_SSH_TOOLKIT_HOME (bot/ssh_toolkit.py's own
# test-isolation env var, same convention abp_agenteval uses for its own state).
# ==================================================================================

@pytest.fixture
def ssh_toolkit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_SSH_TOOLKIT_HOME", str(tmp_path))
    return tmp_path


def test_ssh_status_reports_available(client, ssh_toolkit_home, capsys):
    code, _ = run(["--json", "ssh", "status"], client)
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["available"] is True, status.get("reason")


def test_ssh_full_lifecycle(client, ssh_toolkit_home, capsys):
    code, _ = run(["--json", "ssh", "list"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []

    code, _ = run(["ssh", "add", "--name", "cli-test-ssh", "--host-name", "10.5.5.5",
                   "--user", "tester", "--identity-file", "C:/fake/key", "--tags", "test"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "ssh", "list"], client)
    assert code == 0
    conns = json.loads(capsys.readouterr().out)
    assert any(c["Name"] == "cli-test-ssh" for c in conns)

    code, _ = run(["--json", "ssh", "show", "cli-test-ssh"], client)
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["HostName"] == "10.5.5.5"

    # An unreachable fake host - a normal, non-error result.
    code, _ = run(["--json", "ssh", "test", "cli-test-ssh"], client)
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reachable"] is False

    code, _ = run(["--json", "ssh", "status-all"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)

    code, _ = run(["--json", "ssh", "visualize"], client)
    assert code == 0
    graph = json.loads(capsys.readouterr().out)
    assert any(n["Connection"]["Name"] == "cli-test-ssh" for n in graph)

    code, _ = run(["ssh", "remove", "cli-test-ssh"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "ssh", "list"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_ssh_run_command_over_a_real_local_loopback(client, ssh_toolkit_home, capsys):
    # Not a live SSH server - just confirms the run path (add -> run -> remove) works
    # and a failure to actually connect surfaces as a real, non-crashing error.
    code, _ = run(["ssh", "add", "--name", "cli-test-run", "--host-name", "127.0.0.1",
                   "--port", "1", "--identity-file", "C:/fake/key"], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["ssh", "run", "cli-test-run", "echo", "hi"], client)
    assert code == 1
    assert capsys.readouterr().err

    run(["ssh", "remove", "cli-test-run"], client)


def test_ssh_check_update_reaches_the_real_repo(client, ssh_toolkit_home, capsys):
    code, _ = run(["--json", "ssh", "check-update"], client)
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result.get("Error") in (None, "")
    assert result["InstalledVersion"]


def test_ssh_auto_update_setting_get_and_set(client, ssh_toolkit_home, capsys):
    code, _ = run(["--json", "ssh", "auto-update"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "never"

    code, _ = run(["--json", "ssh", "auto-update", "notify"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "notify"

    code, _ = run(["--json", "ssh", "auto-update"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "notify"
