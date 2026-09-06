"""bot.slash_commands.resolve_command — alias resolution for the single
Telegram command dispatch entry point (bot/handlers.py's on_command).
"""

from __future__ import annotations

from bot import slash_commands


def test_menu_resolves_to_the_model_command():
    # Real Hermes Agent has no native "/menu" command either (confirmed
    # by reading its source) — this alias exists purely so a user's
    # muscle memory from other agent tools still lands on BotServer's
    # real interactive model picker instead of "Unknown command".
    assert slash_commands.resolve_command("menu") == "model"


def test_model_resolves_to_itself():
    assert slash_commands.resolve_command("model") == "model"


def test_unknown_command_resolves_to_none():
    assert slash_commands.resolve_command("not_a_real_command") is None
