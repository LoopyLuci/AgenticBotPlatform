"""Matrix platform wiring outside bot/platforms/matrix_platform.py itself:
credential validation (bot/validators.py), bot instance creation
(bot/bot_instances.py), and the allowed-id type used for comparisons
(bot/platform_supervisor.py) — all against a real temp_db, not mocks.
"""

from __future__ import annotations

import pytest

from bot import bot_instances, platform_supervisor, validators


def test_validate_matrix_homeserver():
    assert validators.validate_matrix_homeserver("https://matrix.org")[0]
    assert validators.validate_matrix_homeserver("matrix.org")[0] is False


def test_validate_matrix_user_id():
    assert validators.validate_matrix_user_id("@bot:matrix.org")[0]
    assert validators.validate_matrix_user_id("bot@matrix.org")[0] is False


def test_validate_matrix_access_token():
    assert validators.validate_matrix_access_token("syt_verylongtoken_abc123")[0]
    assert validators.validate_matrix_access_token("short")[0] is False


def test_matrix_in_platform_token_validators():
    assert "matrix" in validators.PLATFORM_TOKEN_VALIDATORS
    assert set(validators.PLATFORM_TOKEN_VALIDATORS["matrix"]) == {"homeserver", "user_id", "access_token"}


def test_create_matrix_instance(temp_db):
    instance_id = bot_instances.create_instance(
        name="matrix-bot", platform="matrix", backend="cli",
        credentials={"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org", "access_token": "syt_abcdefghij"},
        allowed_user_ids=["@alice:matrix.org"],
    )
    row = bot_instances.get_instance(instance_id)
    assert row["platform"] == "matrix"
    assert row["credentials"]["homeserver"] == "https://matrix.org"
    assert row["allowed_user_ids"] == ["@alice:matrix.org"]


def test_create_matrix_instance_rejects_bad_homeserver(temp_db):
    with pytest.raises(bot_instances.ValidationError):
        bot_instances.create_instance(
            name="bad-matrix-bot", platform="matrix", backend="cli",
            credentials={"homeserver": "not-a-url", "user_id": "@bot:matrix.org", "access_token": "syt_abcdefghij"},
            allowed_user_ids=["@alice:matrix.org"],
        )


def test_create_matrix_instance_rejects_missing_field(temp_db):
    with pytest.raises(bot_instances.ValidationError):
        bot_instances.create_instance(
            name="bad-matrix-bot2", platform="matrix", backend="cli",
            credentials={"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"},
            allowed_user_ids=["@alice:matrix.org"],
        )


def test_build_credentials_set_uses_strings_for_matrix():
    row = {"platform": "matrix", "allowed_user_ids": ["@alice:matrix.org", "@bob:matrix.org"]}
    result = platform_supervisor._build_credentials_set(row)
    assert result == {"@alice:matrix.org", "@bob:matrix.org"}


def test_matrix_runner_registered():
    assert "matrix" in platform_supervisor._RUNNERS
