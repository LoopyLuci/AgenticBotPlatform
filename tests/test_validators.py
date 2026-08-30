"""bot/validators.py's validate_field() dispatcher — the one piece of new
logic behind the web Add-a-bot form's (and bot/tui/'s) live per-field
validation, so a typo in the dispatch (wrong dict key, wrong call
signature) fails here rather than only as a silently-broken UI.
"""

from __future__ import annotations

from bot.validators import validate_field


def test_validate_field_dispatches_to_the_right_validator():
    ok, msg = validate_field("telegram", "bot_token", "123456789:AAExampleTokenFromBotFather1234")
    assert ok
    assert "valid" in msg.lower()


def test_validate_field_reports_invalid_value():
    ok, msg = validate_field("telegram", "bot_token", "not-a-token")
    assert not ok
    assert msg


def test_validate_field_unknown_platform():
    ok, msg = validate_field("carrier-pigeon", "bot_token", "x")
    assert not ok
    assert "carrier-pigeon" in msg


def test_validate_field_unknown_field_for_known_platform():
    ok, msg = validate_field("telegram", "app_token", "x")
    assert not ok
    assert "app_token" in msg


def test_validate_field_matrix_access_token():
    ok, _msg = validate_field("matrix", "access_token", "a-long-enough-token-string")
    assert ok
