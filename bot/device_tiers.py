"""Per-device permission tiers for the conversational admin surface
(Server Chat's BotServer pipeline, Support Bot's new admin actions, and
device/pairing management itself) — see the "Admin control surface" plan.

Deliberately narrow and separate from the pre-existing dashboard REST
API's own flat "any paired device = desktop parity" model
(bot/dashboard/server.py's _identify_caller) — that's an existing,
already-deliberate decision this module does not touch or override.

Four ordered tiers: a device with nothing configured (`none`) gets no
admin conversational capability at all; `standard` matches the Telegram
admin bot's own tool set; `elevated` adds device/pairing management and
destructive local operations; `unrestricted` additionally skips the
per-call dangerous-tool approval prompt for run_shell/write_file, since
the tier assignment itself already represents standing consent.
"""
from __future__ import annotations

TIERS = ("none", "standard", "elevated", "unrestricted")

TIER_RANK = {tier: i for i, tier in enumerate(TIERS)}


def is_valid_tier(tier: str) -> bool:
    return tier in TIER_RANK


def can_manage(actor_tier: str, target_tier: str, *, is_self: bool) -> bool:
    """May a device at actor_tier change the tier of (or revoke) a device
    currently at target_tier? A device can never manage itself, and can
    only ever manage a STRICTLY lower-ranked device — never a peer, never
    a superior. The desktop dashboard token is not subject to this check
    at all (it's the unconditional top authority); callers should bypass
    this function entirely for a desktop caller rather than pass it in."""
    if is_self:
        return False
    return TIER_RANK.get(actor_tier, 0) > TIER_RANK.get(target_tier, 0)


def can_mint(actor_tier: str, new_tier: str) -> bool:
    """May a device at actor_tier mint a brand-new device's key at
    new_tier? A device may create a peer (same tier) but never a
    superior. The desktop dashboard token bypasses this check entirely."""
    return TIER_RANK.get(new_tier, 0) <= TIER_RANK.get(actor_tier, 0)
