"""bot/device_tiers.py — pure rank-comparison functions backing the
per-device permission-tier system (see the "Admin control surface" plan).
"""
from __future__ import annotations

from bot import device_tiers


def test_tier_rank_ascending():
    assert device_tiers.TIER_RANK["none"] < device_tiers.TIER_RANK["standard"]
    assert device_tiers.TIER_RANK["standard"] < device_tiers.TIER_RANK["elevated"]
    assert device_tiers.TIER_RANK["elevated"] < device_tiers.TIER_RANK["unrestricted"]


def test_is_valid_tier():
    for tier in device_tiers.TIERS:
        assert device_tiers.is_valid_tier(tier)
    assert not device_tiers.is_valid_tier("superadmin")


def test_can_manage_refuses_self():
    assert device_tiers.can_manage("unrestricted", "none", is_self=True) is False


def test_can_manage_refuses_peer_or_superior():
    assert device_tiers.can_manage("elevated", "elevated", is_self=False) is False
    assert device_tiers.can_manage("standard", "elevated", is_self=False) is False


def test_can_manage_allows_strictly_lower():
    assert device_tiers.can_manage("elevated", "standard", is_self=False) is True
    assert device_tiers.can_manage("unrestricted", "none", is_self=False) is True


def test_can_mint_allows_peer_not_superior():
    assert device_tiers.can_mint("standard", "standard") is True
    assert device_tiers.can_mint("standard", "elevated") is False
    assert device_tiers.can_mint("unrestricted", "elevated") is True


def test_can_mint_allows_any_lower():
    assert device_tiers.can_mint("unrestricted", "none") is True
