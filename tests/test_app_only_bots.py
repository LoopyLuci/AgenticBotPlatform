"""The "app" platform: a bot instance with no external chat platform at all, reachable only
via POST /api/chat/send-to-bot (the same route the Android app and the CLI/TUI already use).
Needs no credentials and no allowed_user_ids, and has no live connection to start/stop."""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, platform_supervisor
from bot.bot_instances import ValidationError

pytestmark = pytest.mark.usefixtures("temp_db")


def test_app_platform_needs_no_credentials_or_allowed_ids():
    iid = bot_instances.create_instance(
        name="app-bot", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[],
    )
    row = bot_instances.get_instance(iid)
    assert row["platform"] == "app" and row["credentials"] == {} and row["allowed_user_ids"] == []


def test_app_platform_accepts_extra_credentials_or_ids_without_complaint():
    # Nothing is required, but nothing is forbidden either - a stray value here is simply unused.
    iid = bot_instances.create_instance(
        name="app-bot-2", platform="app", backend="native_agent", credentials={"whatever": "x"},
        allowed_user_ids=["someone"],
    )
    assert bot_instances.get_instance(iid) is not None


def test_every_other_platforms_validation_is_unchanged():
    with pytest.raises(ValidationError, match="requires 'bot_token'"):
        bot_instances.create_instance(name="tg", platform="telegram", backend="api", credentials={}, allowed_user_ids=[1])
    with pytest.raises(ValidationError, match="at least one allowed user id"):
        bot_instances.create_instance(
            name="tg2", platform="telegram", backend="api", credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
            allowed_user_ids=[],
        )


def test_unknown_platform_is_still_refused():
    with pytest.raises(ValidationError, match="unknown platform"):
        bot_instances.create_instance(name="x", platform="not-a-real-platform", backend="api", credentials={}, allowed_user_ids=[1])


def test_starting_an_app_only_bot_is_a_no_op(monkeypatch):
    iid = bot_instances.create_instance(
        name="app-bot-3", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[],
    )
    row = bot_instances.get_instance(iid)

    def _fail(*a, **kw):
        raise AssertionError("no runner should ever be looked up for the app platform")
    monkeypatch.setattr(platform_supervisor, "_RUNNERS", {"app": _fail})   # would raise if start_instance ever consulted it
    asyncio.run(platform_supervisor.start_instance(row))
    assert not platform_supervisor.is_running(iid)   # never started a process - nothing to be "running"
