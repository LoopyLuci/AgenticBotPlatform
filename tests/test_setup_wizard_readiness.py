"""bot/setup_wizard.py's per-backend readiness checks — what bot/router.py's ask() consults before every attempt
(check_backend_ready) so a missing prerequisite is a clear directive instead of a raw subprocess FileNotFoundError
three layers down. opencode/openclaw previously had no entry at all, so an instance configured with either always
reported "ready" regardless of whether the binary actually existed."""
from __future__ import annotations

from bot import setup_wizard


def test_every_selectable_backend_has_a_readiness_check():
    from bot.router import VALID_BACKENDS

    missing = [b for b in VALID_BACKENDS if b not in setup_wizard._READINESS_CHECKS]
    assert missing == []


def test_opencode_and_openclaw_report_not_ready_when_the_binary_is_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, reason = setup_wizard.check_backend_ready("opencode")
    assert ok is False and "opencode" in reason and "not found on PATH" in reason
    ok, reason = setup_wizard.check_backend_ready("openclaw")
    assert ok is False and "openclaw" in reason


def test_opencode_reports_ready_when_the_binary_is_found(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    ok, reason = setup_wizard.check_backend_ready("opencode")
    assert ok is True and reason == ""


def test_a_custom_configured_binary_name_is_what_gets_checked(monkeypatch):
    import shutil

    from bot.config import config

    monkeypatch.setattr(config, "_data", {"backends": {"opencode": {"binary": "my-opencode-fork"}}})
    seen = []
    monkeypatch.setattr(shutil, "which", lambda name: seen.append(name) or None)
    ok, reason = setup_wizard.check_backend_ready("opencode")
    assert seen == ["my-opencode-fork"]
    assert "my-opencode-fork" in reason


def test_an_unknown_backend_name_is_reported_ready_with_no_reason():
    assert setup_wizard.check_backend_ready("not-a-real-backend") == (True, "")
