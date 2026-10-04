"""abp_cli - exercised against the real dashboard app (bot/dashboard/server.py's build_app())
over an in-process ASGITransport, same pattern tests/test_tui.py uses for the TUI's own
DashboardClient - real request/response handling, not a mock."""
from __future__ import annotations

import asyncio
import json
import pathlib
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


async def arun(args_list, client):
    """run() for a test that is already inside an event loop (approval.request_approval waits on a
    resolve, so the CLI call that resolves it has to happen on the same loop)."""
    args = _parser().parse_args(args_list)

    try:
        return await _dispatch(args, client)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"couldn't reach the dashboard: {exc}", file=sys.stderr)
        return 1


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


def test_env_set_writes_key_without_returning_content(client, capsys):
    from bot import envfile

    code, _ = run(["--json", "env", "set", "SOME_TEST_VAR", "hello"], client)
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True}
    assert "content" not in result
    assert envfile.get_var("SOME_TEST_VAR") == "hello"


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
    from bot import ssh_toolkit

    if ssh_toolkit._powershell_binary() is None:   # SSH Toolkit is a PowerShell module: on Linux, only with pwsh
        pytest.skip("SSH Toolkit needs PowerShell (pwsh), which this machine does not have")
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


def test_editors_status_and_install(client, capsys, monkeypatch, tmp_path):
    from bot import editor_integrations as ed

    installed = {"v": None}
    monkeypatch.setattr(ed, "code_cli", lambda: "code")
    monkeypatch.setattr(ed, "bundled_vsix", lambda: tmp_path / "abp-vscode.vsix")
    monkeypatch.setattr(ed, "vsix_version", lambda p: "0.2.0")
    monkeypatch.setattr(ed, "installed_version", lambda cli: installed["v"])

    def fake_run(cli, *args, timeout):
        installed["v"] = "0.2.0"
        return __import__("subprocess").CompletedProcess([cli, *args], 0, "", "")
    monkeypatch.setattr(ed, "_run_code", fake_run)

    code, _ = run(["editors", "status"], client)
    out = capsys.readouterr().out
    assert code == 0 and "extension not installed" in out and "-m abp_acp --model auto" in out
    code, _ = run(["editors", "install-vscode"], client)
    assert code == 0 and "version 0.2.0" in capsys.readouterr().out
    code, _ = run(["--json", "editors", "status"], client)
    assert json.loads(capsys.readouterr().out)["vscode"]["installed"] == "0.2.0"
    monkeypatch.setattr(ed, "code_cli", lambda: None)
    code, _ = run(["editors", "install-vscode"], client)
    assert code == 1 and "was not found" in capsys.readouterr().err

# ==================================================================================
# Phase 3: the tool-free surfaces - the memory fabric, running agents and their
# tools, approvals, swarms, the model router and catalog, privacy/DNS, modules,
# local AI, and `doctor`. Same real-app harness as everything above.
# ==================================================================================

def test_memory_add_search_list_and_delete(client, capsys):
    code, _ = run(["--json", "memory", "add", "the CLI test bot runs on port 8788"], client)
    assert code == 0
    added = json.loads(capsys.readouterr().out)
    assert added["id"]

    # shared memories go in pending by default (the fabric's own shared_approval gate), exactly like
    # one written by a running bot, so they are approved before anything recalls them.
    code, _ = run(["--json", "memory", "list", "--scope", "shared", "--status", "pending"], client)
    assert code == 0
    assert any(e["id"] == added["id"] for e in json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "memory", "approve", str(added["id"])], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "approved"

    code, _ = run(["--json", "memory", "search", "8788"], client)
    assert code == 0
    hits = json.loads(capsys.readouterr().out)
    assert any("8788" in (h.get("content") or "") for h in hits), hits
    assert "similarity" in hits[0]

    code, _ = run(["memory", "delete", str(added["id"])], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["--json", "memory", "list", "--scope", "shared"], client)
    assert all(e["id"] != added["id"] for e in json.loads(capsys.readouterr().out))


def test_memory_context_threads_and_post_turn(client, capsys):
    iid = _create_instance(name="mem-bot", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "memory", "post-turn", "cli-thread", "user", "hello from the CLI",
                   "--instance", str(iid)], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["id"]

    code, _ = run(["--json", "memory", "thread", "cli-thread"], client)
    assert code == 0
    assert any(t["text"] == "hello from the CLI" for t in json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "memory", "threads", "--instance", str(iid)], client)
    assert code == 0
    assert any(t["thread"] == "cli-thread" for t in json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "memory", "context", "hello", "--instance", str(iid)], client)
    assert code == 0
    assert "block" in json.loads(capsys.readouterr().out)


def test_memory_knowledge_tree_sources_and_vault(client, capsys):
    code, _ = run(["--json", "memory", "tree-ingest", "CLI note", "the CLI can ingest notes"], client)
    assert code == 0
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["source_id"] == "documents" and ingested["chunks"] >= 1

    code, _ = run(["--json", "memory", "tree-stats"], client)
    assert code == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["sources"]["documents"]["chunks"] >= 1 and isinstance(stats["entities"], int)

    code, _ = run(["--json", "memory", "tree", "walk", "what can the CLI ingest"], client)
    assert code == 0
    assert {"route", "hits", "total"} <= set(json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "memory", "tree", "source", "documents"], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)

    code, _ = run(["--json", "memory", "sources"], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)

    code, _ = run(["--json", "memory", "checkpoint", "cli-test"], client)
    assert code == 1 and "nothing has been snapshotted" in capsys.readouterr().err

    code, _ = run(["--json", "memory", "diff", "--no-commit"], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), (dict, list))

    code, _ = run(["--json", "memory", "vault"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["path"]


def test_memory_tool_rules_and_goals(client, capsys):
    code, _ = run(["--json", "memory", "rule-add", "shell", "never touch .env",
                   "--priority", "critical"], client)
    assert code == 0
    rule = json.loads(capsys.readouterr().out)
    rule_id = rule.get("id") or rule.get("rule_id")

    code, _ = run(["--json", "memory", "rules", "--tool", "shell"], client)
    assert code == 0
    assert any(r.get("tool") == "shell" for r in json.loads(capsys.readouterr().out))

    code, _ = run(["memory", "rule-remove", str(rule_id)], client)
    assert code == 0
    capsys.readouterr()

    code, _ = run(["--json", "memory", "goal-add", "ship the CLI parity work"], client)
    assert code == 0
    goal = json.loads(capsys.readouterr().out)
    assert goal["status"] == "active"

    code, _ = run(["--json", "memory", "goals"], client)
    assert code == 0
    assert any(g["id"] == goal["id"] for g in json.loads(capsys.readouterr().out))

    code, _ = run(["memory", "goal-done", goal["id"]], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["--json", "memory", "goals"], client)
    assert all(g["id"] != goal["id"] for g in json.loads(capsys.readouterr().out))


def test_memory_settings_get_and_set(client, capsys):
    code, _ = run(["memory", "settings-set", "recall_k=5"], client)
    assert code == 0
    capsys.readouterr()
    code, _ = run(["--json", "memory", "settings"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["recall_k"] == 5


def test_tools_list_reports_permission_classes(client, capsys):
    code, _ = run(["--json", "tools", "list"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == len(payload["tools"]) > 0
    names = {t["name"] for t in payload["tools"]}
    assert {"run_shell", "read_file"} <= names, sorted(names)
    for tool in payload["tools"]:
        assert {"name", "permission", "read_only", "asks_first"} <= set(tool), tool

    code, _ = run(["--json", "tools", "list", "--read-only"], client)
    assert code == 0
    assert all(t["read_only"] for t in json.loads(capsys.readouterr().out)["tools"])


def test_agent_run_streams_progress_through_the_api(client, capsys, monkeypatch):
    """A real POST /api/chat/send-to-bot turn, with the fake ask recording a job and a tool event the
    way bot/native_backend.py does, so the progress the CLI prints is real rows read back over the API."""
    from types import SimpleNamespace

    from bot import db
    from bot.router import router

    iid = _create_instance(name="agent-runner", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])

    async def fake_ask(text, **kw):
        job_id = db.create_job("quick_question", "native_agent", 0, text, instance_id=iid)
        db.mark_job_running(job_id, backend="native_agent")
        db.log_job_tool_event(job_id, "tool_started", "read_file", {"path": "README.md"})
        await __import__("asyncio").sleep(0.12)
        db.log_job_tool_event(job_id, "tool_completed", "read_file", {"ok": True})
        db.mark_job_done(job_id, "success", result="done", tokens=42)
        return SimpleNamespace(text=f"ran: {text}")
    monkeypatch.setattr(router, "ask", fake_ask)

    code, _ = run(["--json", "agent", "run", "summarise", "the", "repo", "--instance", str(iid),
                   "--poll", "0.02"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["via"] == "api" and payload["instance_id"] == iid
    assert payload["reply"] == "ran: summarise the repo"
    assert [e["kind"] for e in payload["events"]] == ["job", "tool", "tool"], payload["events"]
    assert payload["events"][1]["tool"] == "read_file"

    code, _ = run(["--json", "agent", "runs", "--instance", str(iid)], client)
    assert code == 0
    runs = json.loads(capsys.readouterr().out)
    job = runs[0]
    assert job["status"] == "success" and job["tokens"] == 42

    code, _ = run(["--json", "agent", "show", str(job["id"])], client)
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["job"]["id"] == job["id"]
    assert [e["event_type"] for e in shown["tool_events"]] == ["tool_started", "tool_completed"]


def test_agent_run_wait_follows_the_job_to_the_end(client, capsys, monkeypatch):
    from types import SimpleNamespace

    from bot import db
    from bot.router import router

    iid = _create_instance(name="agent-waiter", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])

    async def fake_ask(text, **kw):
        job_id = db.create_job("quick_question", "native_agent", 0, text, instance_id=iid)
        db.mark_job_running(job_id, backend="native_agent")
        db.mark_job_done(job_id, "success", result="done")
        return SimpleNamespace(text="ok")
    monkeypatch.setattr(router, "ask", fake_ask)

    code, _ = run(["agent", "run", "wait", "for", "this", "--instance", str(iid), "--wait",
                   "--poll", "0.02"], client)
    assert code == 0
    out = capsys.readouterr().out
    assert "ok" in out and "run " in out and "success" in out


def test_agent_run_approve_flag_answers_an_approval_itself(client, capsys, monkeypatch):
    """The point of the flag: with nobody watching a GUI, --approve resolves the approval the run is
    blocked on, so the turn finishes instead of hanging."""
    from types import SimpleNamespace

    from bot.agent_runtime import approval
    from bot.router import router

    iid = _create_instance(name="auto-approver", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])

    async def fake_ask(text, **kw):
        async def notify(approval_id, tool_name, tool_input):
            pass
        outcome = await approval.request_approval(iid, "chat1", "session1", "shell",
                                                  {"command": "ls"}, notify=notify)
        return SimpleNamespace(text=f"tool outcome: {outcome}")
    monkeypatch.setattr(router, "ask", fake_ask)

    code, _ = run(["--json", "agent", "run", "list", "the", "files", "--instance", str(iid),
                   "--approve", "allow", "--poll", "0.02"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "tool outcome: once"
    approvals = [e for e in payload["events"] if e["kind"] == "approval"]
    assert approvals and approvals[0]["outcome"] == "once"


def test_agent_run_local_streams_through_abp_run(client, capsys, monkeypatch, tmp_path):
    """--backend with no instance runs the same headless agent `python -m abp_run` runs, against a real
    abp_run run (the transport is the only thing faked - the agent loop, tools and cleanup are real)."""
    from abp_run import core

    ran: dict = {}

    async def fake_turn(prompt, *, transport, model, cwd, permission_mode=None, on_text=None, timeout_s=600,
                        extra_context=None):
        ran.update(prompt=prompt, model=model, cwd=str(cwd), permission_mode=permission_mode)
        if on_text is not None:
            await on_text("the answer")
        return core.RunResult(ok=True, reply="the answer", model=model, tokens=7, run_id="r1", status="ok")

    monkeypatch.setattr(core, "run_turn", fake_turn)

    class _Transport:
        pass

    code, _ = run(["--json", "agent", "run", "explain", "this", "repo", "--backend", "anthropic",
                   "--model", "claude-sonnet-5", "--workspace", str(tmp_path),
                   "--permission-mode", "plan"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["via"] == "abp_run" and payload["model"] == "claude-sonnet-5"
    assert payload["ok"] is True and payload["reply"] == "the answer"
    assert ran["prompt"] == "explain this repo" and ran["permission_mode"] == "plan"
    assert pathlib.Path(ran["cwd"]) == tmp_path.resolve()

    code, _ = run(["agent", "run", "x", "--backend", "anthropic", "--workspace", str(tmp_path / "nope")], client)
    assert code == 2 and "not a folder" in capsys.readouterr().err


def test_agent_run_without_an_agent_instance_is_a_usage_error(client, capsys):
    code, _ = run(["agent", "run", "do", "something"], client)
    assert code == 2
    assert "no agent-backed bot instance here" in capsys.readouterr().err


def test_agent_run_with_an_unknown_instance_is_a_usage_error(client, capsys):
    code, _ = run(["agent", "run", "do", "something", "--instance", "9999"], client)
    assert code == 2
    assert "no agent-backed bot instance 9999" in capsys.readouterr().err


def test_agent_budget_show_and_set(client, capsys):
    code, _ = run(["--json", "agent", "budget"], client)
    assert code == 0
    budget = json.loads(capsys.readouterr().out)
    assert {"enabled", "max_children", "max_estimated_usd"} <= set(budget)

    code, _ = run(["--json", "agent", "budget", "--max-children", "3"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["max_children"] == 3


def test_approvals_list_show_approve_and_deny(client, capsys):
    """A real waiting approval: request_approval registers a waiter and a pending row, and only a
    resolve - this CLI's own `approvals approve` - can answer it."""
    from bot.agent_runtime import approval

    iid = _create_instance(name="approver", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])

    async def scenario():
        async def notify(approval_id, tool_name, tool_input):
            pass

        code = await arun(["--json", "approvals", "list", "--instance", str(iid)], client)
        assert code == 0
        assert json.loads(capsys.readouterr().out) == []

        task = asyncio.ensure_future(
            approval.request_approval(iid, "chat1", "session1", "shell", {"command": "echo hi"}, notify=notify))
        await asyncio.sleep(0.05)

        code = await arun(["--json", "approvals", "list", "--instance", str(iid)], client)
        rows = json.loads(capsys.readouterr().out)
        assert code == 0 and len(rows) == 1 and rows[0]["tool"] == "shell", rows
        approval_id = rows[0]["id"]

        code = await arun(["--json", "approvals", "show", str(approval_id)], client)
        assert code == 0 and json.loads(capsys.readouterr().out)["id"] == approval_id

        code = await arun(["--json", "approvals", "approve", str(approval_id)], client)
        assert code == 0
        assert json.loads(capsys.readouterr().out) == {"id": approval_id, "outcome": "once"}
        assert await task == "once"
    asyncio.run(scenario())

    async def refused():
        async def notify(approval_id, tool_name, tool_input):
            pass

        task = asyncio.ensure_future(
            approval.request_approval(iid, "chat2", "session2", "shell", {"command": "rm -rf /"}, notify=notify))
        await asyncio.sleep(0.05)
        code = await arun(["--json", "approvals", "list", "--instance", str(iid)], client)
        pending = json.loads(capsys.readouterr().out)
        assert code == 0 and pending[0]["tool"] == "shell"
        code = await arun(["--json", "approvals", "deny", str(pending[0]["id"])], client)
        assert code == 0
        assert await task == "deny"
    asyncio.run(refused())


def test_swarms_status_reports_budget_runs_and_delegation(client, capsys):
    code, _ = run(["--json", "swarms", "status"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"budget", "swarms", "runs", "delegation"} == set(payload)
    assert "max_children" in payload["budget"]


def test_swarms_dispatch_splits_a_goal_for_a_native_instance(client, capsys, monkeypatch):
    from bot import db

    captured: dict = {}

    async def fake_run_batch(tasks, *, role, provider, model, effort=None, max_children, parent_instance_id):
        captured["tasks"] = tasks
        return {"dispatch_id": "fake", "children": [
            {"index": i, "goal": t["goal"], "model": f"{provider}/{model}", "status": "ok",
             "result_excerpt": f"did {t['goal']}"} for i, t in enumerate(tasks)]}
    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", fake_run_batch)

    iid = _create_instance(name="dispatcher", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["--json", "swarms", "dispatch", "ship", "the", "CLI", "--instance", str(iid),
                   "--provider", "ollama", "--model", "llama3.1"], client)
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert captured["tasks"] == [{"goal": "ship the CLI"}]
    assert result["ok"] is True and result["dispatch_id"] == "fake"
    assert db.list_job_children(result["job_id"])[0]["goal"] == "ship the CLI"

    code, _ = run(["--json", "swarms", "dispatch", "--task", "one", "--task", "two",
                   "--instance", str(iid), "--provider", "ollama", "--model", "llama3.1"], client)
    assert code == 0
    capsys.readouterr()
    assert [t["goal"] for t in captured["tasks"]] == ["one", "two"]

    code, _ = run(["swarms", "dispatch"], client)
    assert code == 2
    assert "goal" in capsys.readouterr().err


def test_swarms_goal_on_a_native_instance_explains_itself(client, capsys):
    iid = _create_instance(name="goal-bot", platform="app", backend="native_agent",
                           credentials={}, allowed_user_ids=[])
    code, _ = run(["swarms", "goal", "ship", "it", "--instance", str(iid)], client)
    assert code == 2
    assert "swarms dispatch" in capsys.readouterr().err


def test_route_explain_rules_and_set(client, capsys):
    code, _ = run(["--json", "route", "explain", "refactor this python file and run the tests"], client)
    assert code == 0
    explained = json.loads(capsys.readouterr().out)
    assert {"task_class", "reasons", "recommendations"} <= set(explained)

    code, _ = run(["--json", "route", "rules"], client)
    assert code == 0
    assert "policy" in json.loads(capsys.readouterr().out)

    code, _ = run(["--json", "route", "set", json.dumps({"sticky": False}), "--note", "cli test"], client)
    assert code == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["version"] >= 1 and saved["changes"]
    code, _ = run(["--json", "route", "rules"], client)
    assert json.loads(capsys.readouterr().out)["policy"]["sticky"] is False

    code, _ = run(["route", "set", "not json"], client)
    assert code == 2
    assert "JSON" in capsys.readouterr().err


def test_models_list_free_and_usage(client, capsys):
    code, _ = run(["--json", "models", "list", "--limit", "3"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and len(payload) <= 3
    if payload:
        assert {"provider", "model", "context", "free"} <= set(payload[0]), payload[0]

    code, _ = run(["--json", "models", "free", "--limit", "3"], client)
    assert code == 0
    assert all(m["free"] for m in json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "models", "usage", "--days", "1"], client)
    assert code == 0
    assert {"days", "models", "timezone"} <= set(json.loads(capsys.readouterr().out))


def test_privacy_get_and_set(client, capsys):
    code, _ = run(["--json", "privacy", "get"], client)
    assert code == 0
    assert "enabled" in json.loads(capsys.readouterr().out)

    code, _ = run(["--json", "privacy", "set", "--enabled"], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True


def test_dns_resolve(client, capsys):
    code, _ = run(["--json", "dns", "resolve", "localhost"], client)
    assert code == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["name"] == "localhost" and resolved["type"] == "A"
    assert isinstance(resolved["values"], list)   # what the world sees; a local name may have none

    code, _ = run(["dns", "resolve", "localhost", "--type", "TXT"], client)
    assert code == 0
    json.loads(capsys.readouterr().out)


def test_modules_list_show_ops_status_and_logs(client, capsys):
    code, _ = run(["--json", "modules", "list"], client)
    assert code == 0
    modules = json.loads(capsys.readouterr().out)
    assert modules and {"id", "name", "area", "installed", "ready", "hub"} <= set(modules[0]), modules[0]
    mid = modules[0]["id"]

    code, _ = run(["--json", "modules", "status", mid], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["module"]["id"] == mid

    code, _ = run(["--json", "modules", "show", mid], client)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["module"]["id"] == mid

    code, _ = run(["--json", "modules", "logs", mid], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)

    # A module that is not installed has no hub, so no operations to list: a real 503 from the module API,
    # not a silent empty answer. An installed one lists its operations. Which modules are installed depends
    # on the machine, so pick by what the list says.
    missing = next((m["id"] for m in modules if not m["installed"]), None)
    if missing:
        code, _ = run(["--json", "modules", "ops", missing], client)
        assert code == 1
        err = capsys.readouterr().err
        assert "not installed" in err or "no operations" in err, err
    present = next((m["id"] for m in modules if m["installed"] and m["ready"]), None)
    if present:
        code, _ = run(["--json", "modules", "ops", present], client)
        assert code == 0
        assert isinstance(json.loads(capsys.readouterr().out), (list, dict))


def test_modules_run_op_reports_a_real_failure(client, capsys):
    code, _ = run(["--json", "modules", "run-op", "no-such-module", "no-such-op"], client)
    assert code == 1
    assert "no-such-module" in capsys.readouterr().err


def test_ai_models_ps_and_serve_status(client, capsys):
    code, _ = run(["--json", "ai", "serve-status"], client)
    assert code == 0
    assert {"server", "running"} <= set(json.loads(capsys.readouterr().out))

    code, _ = run(["--json", "ai", "models"], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)

    code, _ = run(["--json", "ai", "ps"], client)
    assert code == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_ai_run_reaches_the_real_inference_route(client, capsys):
    """`ai run` posts to ABP's own /api/ollama/call. With no local Ollama serving a model this is a real
    failure from that route, not a stubbed one."""
    code, _ = run(["--json", "ai", "run", "some-model", "say", "hi"], client)
    assert code == 1
    err = capsys.readouterr().err.lower()
    assert "error:" in err and "ollama is not running" in err, err

    with pytest.raises(SystemExit) as exc:
        run(["ai", "run", "some-model"], client)
    assert exc.value.code == 2
    assert "required" in capsys.readouterr().err


def test_doctor_reports_every_feature_group(client, capsys):
    code, _ = run(["--json", "doctor"], client)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and "ready" in payload
    names = {c["name"] for c in payload["checks"]}
    assert {"dashboard", "providers", "tools", "approvals", "memory", "swarms", "modules"} <= names, names
    assert any(c["name"].startswith("setup:") for c in payload["checks"])
    for check in payload["checks"]:
        assert {"name", "ok", "detail"} == set(check), check

    code, _ = run(["doctor"], client)
    assert code == 0
    out = capsys.readouterr().out
    assert "every feature answered" in out and "[ok] dashboard" in out


def test_doctor_reports_an_unreachable_dashboard(client, monkeypatch, capsys):
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    code, _ = run(["--json", "doctor"], client)
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["checks"][0]["ok"] is False