"""Slash-command permission tiers — admin vs. user vs. unrestricted.

Modeled on the real Hermes Agent's gateway/slash_access.py (confirmed by
reading that source): gating is off entirely for an instance until an
admin list is configured for it, admins can run everything, and non-admins
get whatever's in their scope's allowed-command list plus a hard floor of
{"help", "start", "whoami"} so they can always find out their own tier.

Simplified from Hermes's two independent per-scope admin lists (DM admin
list and group admin list) to one admin_user_ids per instance — BotServer
instances are already single-owner-scoped (bot_instances.allowed_user_ids
gates who can talk to the bot at all), so a bot's admin(s) don't
meaningfully differ between a DM and a group the bot is also in. The
DM/group split *is* kept for user_allowed_commands, matching Hermes,
because "what a non-admin may run in a group" and "...in a DM" are
genuinely different policies worth setting separately (stored in
bot_instances.action_overrides["slash_access"] — that column is already
designed for exactly this kind of growth, see bot/db.py's schema comment).
"""

from __future__ import annotations

from typing import Any, Optional

_ALWAYS_ALLOWED = {"help", "start", "whoami"}


def _normalize_id(user_id: Any, platform: str) -> Any:
    if platform in ("telegram", "discord"):
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return user_id
    return str(user_id)


def _normalized_set(ids: list[Any], platform: str) -> set[Any]:
    return {_normalize_id(i, platform) for i in (ids or [])}


def is_admin(instance: dict, user_id: Any) -> bool:
    admin_ids = instance.get("admin_user_ids") or []
    if not admin_ids:
        return False
    platform = instance.get("platform", "telegram")
    return _normalize_id(user_id, platform) in _normalized_set(admin_ids, platform)


def tier(instance: dict, user_id: Any) -> str:
    """'unrestricted' — no admin list configured for this instance, gating
    is off, everyone (who's already on allowed_user_ids) can run anything.
    'admin' / 'user' — gating is on; which side of it this user falls on."""
    if not (instance.get("admin_user_ids") or []):
        return "unrestricted"
    return "admin" if is_admin(instance, user_id) else "user"


def _user_allowed_commands(instance: dict, scope: str) -> Optional[list[str]]:
    slash_cfg = (instance.get("action_overrides") or {}).get("slash_access") or {}
    key = "dm_user_commands" if scope == "dm" else "group_user_commands"
    cmds = slash_cfg.get(key)
    if cmds is None and scope == "group":
        # DM list falls through to group scope so an operator only has to
        # configure it once, matching Hermes's exact fallthrough direction.
        cmds = slash_cfg.get("dm_user_commands")
    return cmds


def allowed_commands(instance: dict, user_id: Any, scope: str) -> Optional[set[str]]:
    """None means "every command is allowed" (unrestricted tier, or this
    user is an admin). Otherwise the concrete set of canonical command
    names this user may run in this scope, always including the floor."""
    t = tier(instance, user_id)
    if t in ("admin", "unrestricted"):
        return None
    return set(_user_allowed_commands(instance, scope) or []) | _ALWAYS_ALLOWED


def can_run(instance: dict, user_id: Any, scope: str, command_name: str) -> bool:
    allowed = allowed_commands(instance, user_id, scope)
    return allowed is None or command_name in allowed
