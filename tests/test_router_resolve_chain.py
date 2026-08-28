"""Router.resolve_chain() is the precedence logic documented at the top of
bot/router.py: explicit override > instance action_override > instance
default > global action_override > global default, with a "ui never gets
a silent default" guard. Pure logic, no live backend/subprocess involved
— exactly the kind of thing that should never need a hand-run scratch
script to verify again.
"""
from __future__ import annotations

import pytest

from bot.config import config
from bot.router import Router


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "default_backend": "cli",
        "action_overrides": {
            "quick_question": {"backend": "api", "backup": ["cli"]},
        },
    })
    return Router()


def test_explicit_override_wins_over_everything(router):
    assert router.resolve_chain("quick_question", backend_override="ui") == ["ui"]


def test_explicit_override_rejects_unknown_backend(router):
    with pytest.raises(ValueError, match="unknown backend"):
        router.resolve_chain("quick_question", backend_override="not-a-real-backend")


def test_global_action_override_with_backup_chain(router):
    assert router.resolve_chain("quick_question") == ["api", "cli"]


def test_falls_back_to_global_default_backend(router):
    assert router.resolve_chain("some_unconfigured_action") == ["cli"]


def test_ui_as_global_default_is_never_used_silently(monkeypatch):
    # If "ui" is the configured global default_backend and nothing
    # explicit asked for it (no override flag, no action_override entry),
    # the guard must swap it for a real fallback rather than silently
    # routing to desktop-window automation no one opted into. With no
    # other backend configured, config.get("default_backend", "api")'s
    # own fallback ("api") is what the guard substitutes.
    monkeypatch.setattr(config, "_data", {"default_backend": "ui", "action_overrides": {}})
    r = Router()
    assert r.resolve_chain("anything") == ["api"]


def test_explicit_action_override_naming_ui_is_not_blocked_by_the_guard(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "default_backend": "cli",
        "action_overrides": {"some_action": {"backend": "ui"}},
    })
    r = Router()
    # The guard only protects the *global default* from being used
    # silently — an explicit action_override naming "ui" is a real,
    # deliberate choice and must be allowed through untouched.
    assert r.resolve_chain("some_action") == ["ui"]


def test_instance_action_override_beats_global(router, monkeypatch):
    from bot import bot_instances

    monkeypatch.setattr(
        bot_instances, "get_instance",
        lambda iid: {"id": iid, "action_overrides": {"quick_question": {"backend": "hermes_cli", "backup": []}}, "backend": "cli"},
    )
    assert router.resolve_chain("quick_question", instance_id=1) == ["hermes_cli"]


def test_instance_own_backend_beats_global_default(router, monkeypatch):
    from bot import bot_instances

    monkeypatch.setattr(
        bot_instances, "get_instance",
        lambda iid: {"id": iid, "action_overrides": {}, "backend": "hermes_gateway"},
    )
    assert router.resolve_chain("quick_question", instance_id=1) == ["hermes_gateway"]


def test_instance_own_ui_backend_is_not_blocked_by_the_guard(router, monkeypatch):
    from bot import bot_instances

    # An instance's own explicit "ui" backend choice is exempt from the
    # "ui never silent" guard — that guard only exists to stop the
    # *global config default* from silently routing to desktop automation,
    # not to block a bot the user deliberately configured for it.
    monkeypatch.setattr(
        bot_instances, "get_instance",
        lambda iid: {"id": iid, "action_overrides": {}, "backend": "ui"},
    )
    assert router.resolve_chain("desktop_specific", instance_id=1) == ["ui"]


def test_nonexistent_instance_falls_back_to_global(router, monkeypatch):
    from bot import bot_instances

    monkeypatch.setattr(bot_instances, "get_instance", lambda iid: None)
    assert router.resolve_chain("quick_question", instance_id=999) == ["api", "cli"]
