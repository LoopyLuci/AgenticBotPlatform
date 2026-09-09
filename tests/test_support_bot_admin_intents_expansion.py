"""Admin control surface plan, Section 4 expansion: device management
(retier, gated revoke/list, tiered minting), hooks, agent_settings, and
auto_manage read access via Support Bot — building on the estop-only
slice in test_support_bot_admin_intents.py.
"""
from __future__ import annotations

import asyncio

from bot import bot_instances, db
from bot.support_bot.engine import SupportBot


def _run(coro):
    return asyncio.run(coro)


def _make_device(label: str, tier: str) -> int:
    key_id, _plaintext = db.create_api_key(label, permission_tier=tier)
    return key_id


def _make_instance(name: str) -> int:
    creds = {"bot_token": "123456789:AAExampleTokenFromBotFatherPaddedToBeLongEnough"}
    return bot_instances.create_instance(name, "telegram", "api", creds, [111])


# ------------------------------------------------------------- devices_list
def test_devices_list_requires_at_least_standard_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("list paired devices", actor="test", device_tier="none"))
    assert "permission tier" in reply.text.lower()


def test_devices_list_readable_at_standard_tier(temp_db):
    _make_device("Phone", "none")
    bot = SupportBot()
    reply = _run(bot.handle("list paired devices", actor="test", device_tier="standard"))
    assert "Phone" in reply.text


# ------------------------------------------------------------ device_revoke
def test_device_revoke_refused_below_elevated_tier(temp_db):
    _make_device("Kindle Fire", "none")
    bot = SupportBot()
    reply = _run(bot.handle("revoke Kindle Fire", actor="test", device_tier="standard"))
    assert "permission tier" in reply.text.lower()


def test_device_revoke_refused_against_a_peer_or_superior_tier(temp_db):
    """ADMIN_ONLY_INTENTS only checks the floor (elevated); the handler's
    own can_manage check is what stops an elevated caller from revoking
    another elevated device — only a strictly lower-tier target may be
    revoked."""
    _make_device("Peer Elevated", "elevated")
    bot = SupportBot()
    reply = _run(bot.handle("revoke Peer Elevated", actor="test", device_tier="elevated"))
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test")) if reply.needs_confirm else reply
    assert "strictly lower" in confirmed.text.lower()
    row = db.get_api_key(db.list_devices()[0]["id"])
    assert row["revoked_at"] is None


def test_device_revoke_succeeds_against_a_strictly_lower_tier_target(temp_db):
    device_id = _make_device("Old Phone", "none")
    bot = SupportBot()
    reply = _run(bot.handle("revoke Old Phone", actor="test", device_tier="elevated"))
    assert reply.needs_confirm is True  # device_revoke is destructive
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert confirmed.applied is True
    row = db.get_api_key(device_id)
    assert row["revoked_at"] is not None


# ------------------------------------------------------------ device_retier
def test_device_retier_refused_below_elevated_tier(temp_db):
    _make_device("Tablet", "none")
    bot = SupportBot()
    reply = _run(bot.handle("set Tablet's permission tier to standard", actor="test", device_tier="standard"))
    assert "permission tier" in reply.text.lower()


def test_device_retier_refused_when_granting_above_callers_own_tier(temp_db):
    _make_device("Tablet", "none")
    bot = SupportBot()
    reply = _run(bot.handle("set Tablet's permission tier to unrestricted", actor="test", device_tier="elevated"))
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert "can't grant tier" in confirmed.text.lower()


def test_device_retier_succeeds_to_a_peer_tier_of_a_strictly_lower_device(temp_db):
    _make_device("Tablet", "standard")
    bot = SupportBot()
    reply = _run(bot.handle("set Tablet's permission tier to elevated", actor="test", device_tier="elevated"))
    assert reply.needs_confirm is True
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert confirmed.applied is True
    row = db.get_api_key(db.list_devices()[0]["id"])
    assert row["permission_tier"] == "elevated"


# --------------------------------------------------------- mobile_key_create
def test_mobile_key_create_with_no_tier_needs_no_permission(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("pair a new device", actor="test", device_tier="none"))
    assert reply.needs_confirm is True  # now destructive regardless of tier
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert confirmed.applied is True
    assert "pairing" in confirmed.text.lower()


def test_mobile_key_create_refuses_a_tier_above_the_callers_own(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("pair a new device at elevated tier", actor="test", device_tier="standard"))
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert "can't mint" in confirmed.text.lower()


def test_mobile_key_create_allows_a_tier_at_or_below_the_callers_own(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("pair a new device at standard tier", actor="test", device_tier="elevated"))
    confirmed = _run(bot.confirm(reply.confirm_token, actor="test"))
    assert confirmed.applied is True
    rows = db.list_devices()
    assert any(r["permission_tier"] == "standard" for r in rows)


# --------------------------------------------------------------------- hooks
def test_hooks_list_requires_at_least_standard_tier(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("list hooks", actor="test", device_tier="none"))
    assert "permission tier" in reply.text.lower()


def test_hooks_list_shows_no_hooks_message_when_empty(temp_db):
    bot = SupportBot()
    reply = _run(bot.handle("list hooks", actor="test", device_tier="standard"))
    assert "no hooks" in reply.text.lower()


def test_hook_enable_disable_remove_require_elevated_tier(temp_db):
    hook_id = db.add_agent_hook("PreToolUse", "echo hi")
    bot = SupportBot()

    refused = _run(bot.handle(f"disable hook {hook_id}", actor="test", device_tier="standard"))
    assert "permission tier" in refused.text.lower()

    disable_reply = _run(bot.handle(f"disable hook {hook_id}", actor="test", device_tier="elevated"))
    assert disable_reply.needs_confirm is True  # hook_disable is destructive
    disabled = _run(bot.confirm(disable_reply.confirm_token, actor="test"))
    assert disabled.applied is True
    assert db.get_agent_hook(hook_id)["enabled"] == 0

    enable_reply = _run(bot.handle(f"enable hook {hook_id}", actor="test", device_tier="elevated"))
    assert enable_reply.applied is True  # hook_enable is not destructive
    assert db.get_agent_hook(hook_id)["enabled"] == 1

    remove_reply = _run(bot.handle(f"remove hook {hook_id}", actor="test", device_tier="elevated"))
    assert remove_reply.needs_confirm is True  # hook_remove is destructive
    removed = _run(bot.confirm(remove_reply.confirm_token, actor="test"))
    assert removed.applied is True
    assert db.get_agent_hook(hook_id) is None


# ----------------------------------------------------------- agent_settings
def test_agent_settings_show_requires_standard_tier_and_hides_admin_flag(temp_db):
    instance_id = _make_instance("BotServer Control")
    bot = SupportBot()

    refused = _run(bot.handle("show agent settings for BotServer Control", actor="test", device_tier="none"))
    assert "permission tier" in refused.text.lower()

    reply = _run(bot.handle("show agent settings for BotServer Control", actor="test", device_tier="standard"))
    assert "worker_provider" in reply.text
    assert "hidden" in reply.text.lower()  # is_admin_instance never surfaced


def test_agent_settings_set_effort_requires_elevated_tier(temp_db):
    from bot import agent_settings

    instance_id = _make_instance("BotServer Control")
    bot = SupportBot()

    refused = _run(bot.handle("set BotServer Control's worker effort to high", actor="test", device_tier="standard"))
    assert "permission tier" in refused.text.lower()

    reply = _run(bot.handle("set BotServer Control's worker effort to high", actor="test", device_tier="elevated"))
    assert reply.applied is True
    assert agent_settings.get(instance_id)["worker_effort"] == "high"


# -------------------------------------------------------------- auto_manage
def test_auto_manage_show_requires_standard_tier(temp_db):
    _make_instance("BotServer Control")
    bot = SupportBot()

    refused = _run(bot.handle("show auto-manage settings for BotServer Control", actor="test", device_tier="none"))
    assert "permission tier" in refused.text.lower()

    reply = _run(bot.handle("show auto-manage settings for BotServer Control", actor="test", device_tier="standard"))
    assert "enabled=" in reply.text.lower()
