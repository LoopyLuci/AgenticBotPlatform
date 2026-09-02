"""/status (bot.commands.cmd_status) and the new /gateway (cmd_gateway):
/status used to dump every backend's readiness regardless of what this bot
is even wired to, and always showed "(backend default)" for the model
instead of resolving what that default actually is. This covers the split
(readiness moved to /gateway, family-filtered) and real_time_model_label's
live resolution — including the two genuinely-readable local settings
files (Claude Code CLI's settings.json, Hermes's config.yaml).
"""
from __future__ import annotations

import json

import pytest

from bot import commands, models
from bot.commands import CmdContext
from bot.config import config


def _ctx(instance_id=None):
    return CmdContext(instance_id=instance_id, instance_name="test", user_id=1, chat_id=1, actor="test")


@pytest.fixture(autouse=True)
def _fixed_config(monkeypatch):
    # No live Anthropic account here — format_model_label must fall back to
    # the bare model id, not a live-fetched "anthropic/..." provider label,
    # for these assertions to be about real_time_model_label's own logic
    # rather than whatever key happens to be in this shell's environment.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "_data", {
        "default_backend": "cli",
        "default_hermes_backend": "hermes_gateway",
        "backends": {},
    })


def _make_instance(temp_db, backend, model=None):
    from bot import bot_instances

    return bot_instances.create_instance(
        name="t", platform="telegram", backend=backend,
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFatherPadding123"},
        allowed_user_ids=[1], model=model,
    )


def test_status_has_no_backend_readiness_dump(temp_db):
    reply = _run(commands.cmd_status(_ctx(), []))
    assert "Backends:" not in reply
    assert "Default backend:" not in reply


def test_status_shows_model_for_bound_instance(temp_db):
    iid = _make_instance(temp_db, "api", model="claude-opus-5")
    reply = _run(commands.cmd_status(_ctx(iid), []))
    assert "Model:" in reply


def test_gateway_scopes_to_claude_family_only(temp_db):
    iid = _make_instance(temp_db, "cli")
    reply = _run(commands.cmd_gateway(_ctx(iid), []))
    assert "claude_api" in reply
    assert "claude_cli" in reply
    assert "claude_ui" in reply
    assert "hermes_cli" not in reply
    assert "hermes_gateway" not in reply
    assert "Default backend: cli" in reply


def test_gateway_scopes_to_hermes_family_only(temp_db):
    iid = _make_instance(temp_db, "hermes_gateway")
    reply = _run(commands.cmd_gateway(_ctx(iid), []))
    assert "hermes_cli" in reply
    assert "hermes_gateway" in reply
    assert "claude_api" not in reply
    assert "claude_cli" not in reply
    assert "claude_ui" not in reply
    assert "Default backend: hermes_gateway" in reply


def test_gateway_shows_everything_with_no_bound_instance(temp_db):
    reply = _run(commands.cmd_gateway(_ctx(None), []))
    assert "claude_api" in reply
    assert "hermes_cli" in reply


def test_real_time_model_label_uses_explicit_override(temp_db):
    instance = {"backend": "api", "model": "claude-opus-5"}
    label = _run(commands.real_time_model_label(instance))
    assert label == "claude-opus-5"


def test_real_time_model_label_api_falls_back_to_default_constant(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {}})
    instance = {"backend": "api", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert label == models.DEFAULT_API_MODEL


def test_real_time_model_label_api_uses_configured_default(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {"api": {"model": "claude-haiku-5"}}})
    instance = {"backend": "api", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert label == "claude-haiku-5"


def test_real_time_model_label_ui_states_its_own_limitation(temp_db):
    instance = {"backend": "ui", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert "not visible to BotServer" in label


def test_real_time_model_label_cli_reads_real_settings_file(temp_db, monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    monkeypatch.setattr(models.Path, "home", classmethod(lambda cls: tmp_path))
    models._cli_settings_cache["mtime"] = None
    instance = {"backend": "cli", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert label == "sonnet (Claude Code CLI's own default)"


def test_real_time_model_label_cli_missing_file_is_honest(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(models.Path, "home", classmethod(lambda cls: tmp_path))
    instance = {"backend": "cli", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert "not detected" in label


def test_real_time_model_label_hermes_reads_real_config_file(temp_db, monkeypatch, tmp_path):
    from bot import hermes_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: grok-4.20-reasoning\n", encoding="utf-8")
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_data", {"backends": {}})
    models._hermes_config_cache["mtime"] = None
    instance = {"backend": "hermes_gateway", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert label == "grok-4.20-reasoning (Hermes's own default)"


def test_real_time_model_label_hermes_dict_shaped_model_field(temp_db, monkeypatch, tmp_path):
    # Real-world shape confirmed against an actual config.yaml: `model:`
    # can be a mapping with the real default under `default`, not a bare
    # string — see bot.models.local_hermes_default_model's docstring.
    from bot import hermes_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: nous\n  default: meituan/longcat-2.0:free\n", encoding="utf-8")
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_data", {"backends": {}})
    models._hermes_config_cache["mtime"] = None
    instance = {"backend": "hermes_gateway", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert label == "meituan/longcat-2.0:free (Hermes's own default)"


def test_real_time_model_label_hermes_blank_field_is_honest(temp_db, monkeypatch, tmp_path):
    from bot import hermes_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n", encoding="utf-8")
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_data", {"backends": {}})
    models._hermes_config_cache["mtime"] = None
    instance = {"backend": "hermes_gateway", "model": None}
    label = _run(commands.real_time_model_label(instance))
    assert "not set" in label


def _run(coro):
    import asyncio

    return asyncio.run(coro)
