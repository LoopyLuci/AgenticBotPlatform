"""/devices (bot.commands.cmd_devices) — paired-device management as a
command, reusing the exact same db.revoke_api_key()/set_api_key_tier()
calls the dashboard's Devices tab buttons already use, not a parallel
implementation.
"""
from __future__ import annotations

import asyncio

from bot import commands, db
from bot.commands import CmdContext


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return CmdContext(instance_id=None, instance_name="", user_id="terminal", chat_id="terminal", actor="test")


def test_list_with_no_devices(temp_db):
    reply = _run(commands.cmd_devices(_ctx(), ["list"]))
    assert "no paired devices" in reply.lower()


def test_list_shows_a_paired_device(temp_db):
    key_id, _plaintext = db.create_api_key("phone", kind="device", permission_tier="standard")
    reply = _run(commands.cmd_devices(_ctx(), ["list"]))
    assert str(key_id) in reply
    assert "standard" in reply


def test_revoke_removes_it_from_the_list(temp_db):
    key_id, _plaintext = db.create_api_key("phone", kind="device", permission_tier="standard")
    reply = _run(commands.cmd_devices(_ctx(), ["revoke", str(key_id)]))
    assert "revoked" in reply.lower()
    reply = _run(commands.cmd_devices(_ctx(), ["list"]))
    assert str(key_id) not in reply


def test_revoke_unknown_device_id(temp_db):
    reply = _run(commands.cmd_devices(_ctx(), ["revoke", "999999"]))
    assert "no such device" in reply.lower()


def test_revoke_non_numeric_id(temp_db):
    reply = _run(commands.cmd_devices(_ctx(), ["revoke", "abc"]))
    assert "not a device id" in reply.lower()


def test_retier_changes_the_tier(temp_db):
    key_id, _plaintext = db.create_api_key("phone", kind="device", permission_tier="standard")
    reply = _run(commands.cmd_devices(_ctx(), ["retier", str(key_id), "elevated"]))
    assert "elevated" in reply.lower()
    reply = _run(commands.cmd_devices(_ctx(), ["list"]))
    assert "elevated" in reply


def test_retier_rejects_an_unknown_tier(temp_db):
    key_id, _plaintext = db.create_api_key("phone", kind="device", permission_tier="standard")
    reply = _run(commands.cmd_devices(_ctx(), ["retier", str(key_id), "godmode"]))
    assert "unknown permission tier" in reply.lower()


def test_usage_line_with_no_args_falls_through_to_list():
    # No args at all currently behaves the same as "list" (matches /mcp's
    # own convention of a bare command defaulting to its list subcommand).
    reply = _run(commands.cmd_devices(_ctx(), []))
    assert "no paired devices" in reply.lower() or ":" in reply


def test_unrecognized_subcommand_shows_usage(temp_db):
    reply = _run(commands.cmd_devices(_ctx(), ["frobnicate"]))
    assert "usage" in reply.lower()
