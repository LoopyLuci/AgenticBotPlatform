"""bot.support_bot.slots — fuzzy/regex argument extraction. The pure
functions (no DB/live-config lookups) get real unit coverage here; the
DB-backed ones (find_bot_name, find_swarm, ...) get their own fixtures.
"""
from __future__ import annotations

from bot.support_bot import slots


class TestFindBackend:
    def test_finds_a_named_backend(self):
        assert slots.find_backend("switch to the cli backend please") == "cli"

    def test_returns_none_when_no_backend_named(self):
        assert slots.find_backend("how's it going today") is None

    def test_first_match_wins_when_multiple_named(self):
        # VALID_BACKENDS order is ("api", "cli", "ui", "hermes_cli", "hermes_gateway")
        assert slots.find_backend("cli or api, whichever") == "api"


class TestFindQuoted:
    def test_extracts_first_quoted_substring(self):
        assert slots.find_quoted('rename it to "my new bot"') == "my new bot"

    def test_none_when_nothing_quoted(self):
        assert slots.find_quoted("rename it to my new bot") is None

    def test_single_quotes_also_work(self):
        assert slots.find_quoted("call it 'night shift'") == "night shift"


class TestFindNumber:
    def test_bare_number(self):
        assert slots.find_number("show me session 5") == 5

    def test_hash_prefixed_number(self):
        assert slots.find_number("check job #17") == 17

    def test_none_when_no_number(self):
        assert slots.find_number("check the latest job") is None

    def test_first_number_wins(self):
        assert slots.find_number("job 3 depends on job 9") == 3


class TestFindBool:
    def test_recognizes_enable_words(self):
        for phrase in ("enable it", "turn on notifications", "yes please", "activate this"):
            assert slots.find_bool(phrase) is True, phrase

    def test_recognizes_disable_words(self):
        for phrase in ("disable it", "turn off notifications", "no thanks", "deactivate this"):
            assert slots.find_bool(phrase) is False, phrase

    def test_none_when_ambiguous(self):
        assert slots.find_bool("what does this setting do") is None


class TestFindAgentControlMode:
    def test_allowlist(self):
        assert slots.find_agent_control_mode("switch to allowlist mode") == "allowlist"

    def test_trust(self):
        assert slots.find_agent_control_mode("just trust everything") == "trust_all"

    def test_none_when_unrecognized(self):
        assert slots.find_agent_control_mode("what mode are we in") is None


class TestCandidatesFromText:
    def test_includes_quoted_phrases_and_individual_words(self):
        candidates = slots._candidates_from_text('disable the "filesystem" mcp server')
        assert "filesystem" in candidates
        assert "disable" in candidates
        assert "mcp" in candidates
