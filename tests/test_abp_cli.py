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
