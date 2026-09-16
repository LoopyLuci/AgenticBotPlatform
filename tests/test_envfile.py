"""bot/envfile.py — DASHBOARD_TOKEN auto-generation.

A fresh install must never require a human to invent/paste a dashboard
token by hand; ensure_dashboard_token() is the fix for that (see
bot/main.py's boot sequence, which calls it right after load_dotenv()).
"""

from __future__ import annotations

from bot import envfile


def test_ensure_dashboard_token_generates_when_missing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_OTHER_VAR=x\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "resolve", lambda: env_path)

    token = envfile.ensure_dashboard_token()

    assert token
    assert len(token) >= 32
    content = env_path.read_text(encoding="utf-8")
    assert "SOME_OTHER_VAR=x" in content
    assert f"DASHBOARD_TOKEN={token}" in content


def test_ensure_dashboard_token_is_idempotent(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(envfile, "resolve", lambda: env_path)

    first = envfile.ensure_dashboard_token()
    second = envfile.ensure_dashboard_token()

    assert first == second
    # Only one DASHBOARD_TOKEN line, not appended twice.
    assert env_path.read_text(encoding="utf-8").count("DASHBOARD_TOKEN=") == 1


def test_ensure_dashboard_token_leaves_an_existing_token_untouched(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("DASHBOARD_TOKEN=already-set-value\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "resolve", lambda: env_path)

    token = envfile.ensure_dashboard_token()

    assert token == "already-set-value"


def test_ensure_dashboard_token_works_on_a_brand_new_install_with_no_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / "nested" / ".env"  # parent dir doesn't exist yet either
    monkeypatch.setattr(envfile, "resolve", lambda: env_path)

    token = envfile.ensure_dashboard_token()

    assert token
    assert env_path.exists()
    assert f"DASHBOARD_TOKEN={token}" in env_path.read_text(encoding="utf-8")
