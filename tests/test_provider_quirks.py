"""bot/agent_runtime/provider_quirks.py — real per-vendor wire quirks
confirmed against Hermes Agent's own per-vendor adapter plugins. A
generic OpenAI-compatible client silently misbehaves or hard-fails on
several real providers without these.
"""
from __future__ import annotations

from bot.agent_runtime import provider_quirks


class TestProfileFor:
    def test_matches_by_catalog_id(self):
        profile = provider_quirks.profile_for("deepseek", "https://api.deepseek.com/v1")
        assert profile is not None
        assert profile.force_thinking_extra_body

    def test_matches_by_base_url_when_catalog_id_is_none(self):
        profile = provider_quirks.profile_for(None, "https://api.moonshot.ai/v1")
        assert profile is not None
        assert profile.mutually_exclusive_thinking_and_effort

    def test_no_match_returns_none(self):
        assert provider_quirks.profile_for("some-unrelated-provider", "https://example.com/v1") is None

    def test_openrouter_clamps_effort(self):
        profile = provider_quirks.profile_for("openrouter", "https://openrouter.ai/api/v1")
        assert profile.clamp_effort_to == {"low", "medium", "high"}


class TestApply:
    def test_none_profile_leaves_payload_untouched(self):
        payload = {"model": "x", "reasoning_effort": "xhigh"}
        provider_quirks.apply(payload, profile=None, effort="xhigh")
        assert payload == {"model": "x", "reasoning_effort": "xhigh"}

    def test_deepseek_forces_thinking_extra_body_present(self):
        profile = provider_quirks.profile_for("deepseek", "https://api.deepseek.com")
        payload = {"model": "deepseek-v4"}
        provider_quirks.apply(payload, profile=profile, effort="high")
        assert payload["extra_body"]["thinking"] == {"type": "enabled"}

    def test_deepseek_disables_thinking_when_effort_is_none(self):
        profile = provider_quirks.profile_for("deepseek", "https://api.deepseek.com")
        payload = {"model": "deepseek-v4"}
        provider_quirks.apply(payload, profile=profile, effort="none")
        assert payload["extra_body"]["thinking"] == {"type": "disabled"}

    def test_kimi_never_sends_both_thinking_and_reasoning_effort(self):
        profile = provider_quirks.profile_for("kimi-coding", "https://api.kimi.com/coding")
        payload = {"model": "kimi-k3", "reasoning_effort": "high"}
        # A prior step (force_thinking_extra_body is False for kimi, so
        # this simulates the generic effort-passthrough already having
        # set extra_body.thinking some other way) — the mutually-
        # exclusive rule must still drop reasoning_effort once
        # extra_body.thinking is present.
        payload["extra_body"] = {"thinking": {"type": "enabled"}}
        provider_quirks.apply(payload, profile=profile, effort="high")
        assert "reasoning_effort" not in payload

    def test_openrouter_clamps_an_out_of_range_effort_down(self):
        profile = provider_quirks.profile_for("openrouter", "https://openrouter.ai/api/v1")
        payload = {"model": "x", "reasoning_effort": "xhigh"}
        provider_quirks.apply(payload, profile=profile, effort="xhigh")
        assert payload["reasoning_effort"] == "high"

    def test_openrouter_leaves_an_in_range_effort_alone(self):
        profile = provider_quirks.profile_for("openrouter", "https://openrouter.ai/api/v1")
        payload = {"model": "x", "reasoning_effort": "medium"}
        provider_quirks.apply(payload, profile=profile, effort="medium")
        assert payload["reasoning_effort"] == "medium"
