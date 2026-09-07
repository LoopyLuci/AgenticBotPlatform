"""bot/effort.py — the canonical effort vocabulary and its honest,
explicit per-backend mappings. See the module docstring for why Hermes's
real 8-level ladder was chosen as the canonical one.
"""
from __future__ import annotations

from bot import effort


def test_ladder_matches_hermes_real_order():
    assert effort.EFFORT_LADDER == ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def test_is_valid():
    assert effort.is_valid("high")
    assert not effort.is_valid("extreme")
    assert not effort.is_valid(None)


class TestToAnthropic:
    def test_passthrough_levels(self):
        for level in ("low", "medium", "high", "xhigh"):
            assert effort.to_anthropic(level) == level

    def test_none_and_minimal_omit_the_field(self):
        assert effort.to_anthropic("none") is None
        assert effort.to_anthropic("minimal") is None
        assert effort.to_anthropic(None) is None

    def test_max_and_ultra_both_collapse_to_max(self):
        """Anthropic's real API has nothing above "max" — "ultra" must
        never be sent verbatim (it would be rejected or misinterpreted)."""
        assert effort.to_anthropic("max") == "max"
        assert effort.to_anthropic("ultra") == "max"

    def test_unrecognized_level_omits_rather_than_guesses(self):
        assert effort.to_anthropic("extreme") is None


class TestToUiBackend:
    def test_passthrough_levels(self):
        assert effort.to_ui_backend("medium") == "medium"
        assert effort.to_ui_backend("high") == "high"
        assert effort.to_ui_backend("max") == "max"

    def test_none_and_minimal_map_to_low(self):
        assert effort.to_ui_backend("none") == "low"
        assert effort.to_ui_backend("minimal") == "low"

    def test_xhigh_maps_to_extra_and_ultra_to_ultracode(self):
        assert effort.to_ui_backend("xhigh") == "extra"
        assert effort.to_ui_backend("ultra") == "ultracode"

    def test_missing_or_unrecognized_falls_back_to_ui_default(self):
        from bot.backends.ui_backend import DEFAULT_EFFORT as UI_DEFAULT

        assert effort.to_ui_backend(None) == UI_DEFAULT
        assert effort.to_ui_backend("extreme") == UI_DEFAULT


class TestToOpenaiReasoningEffort:
    def test_passthrough_low_medium_high(self):
        assert effort.to_openai_reasoning_effort("low") == "low"
        assert effort.to_openai_reasoning_effort("medium") == "medium"
        assert effort.to_openai_reasoning_effort("high") == "high"

    def test_none_omits_the_field(self):
        assert effort.to_openai_reasoning_effort("none") is None
        assert effort.to_openai_reasoning_effort(None) is None

    def test_minimal_maps_to_low(self):
        assert effort.to_openai_reasoning_effort("minimal") == "low"

    def test_above_high_collapses_to_high_not_sent_verbatim(self):
        """Real risk: sending "xhigh"/"ultra" verbatim to a provider that
        strictly validates this enum could 400 — collapse instead of
        guessing a value OpenAI's own API doesn't define."""
        assert effort.to_openai_reasoning_effort("xhigh") == "high"
        assert effort.to_openai_reasoning_effort("max") == "high"
        assert effort.to_openai_reasoning_effort("ultra") == "high"


class TestToHermes:
    def test_identity_for_every_real_level(self):
        for level in effort.EFFORT_LADDER:
            assert effort.to_hermes(level) == level

    def test_none_or_unrecognized_means_inherit(self):
        assert effort.to_hermes(None) is None
        assert effort.to_hermes("extreme") is None
