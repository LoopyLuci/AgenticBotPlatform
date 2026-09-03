"""Phase 9: parsing dispatch_swarm_goal's structured per-child breakdown
out of a dispatch's own final reply (see bot/swarm/prompts.py's
hermes_delegation_goal for the instruction that asks for this, and
bot/swarm/child_parser.py's module docstring for why this must never
raise — a missing/malformed block just means "no breakdown available",
the same as before this feature existed).
"""

from __future__ import annotations

from bot.swarm.child_parser import parse_child_breakdown

WELL_FORMED = """
Here is the synthesized answer covering everything.

```json
[
  {"index": 0, "goal": "write the intro", "model": "openrouter/free-model", "status": "ok", "result_excerpt": "Done, 3 paragraphs."},
  {"index": 1, "goal": "write the conclusion", "model": "openrouter/free-model", "status": "error", "result_excerpt": "Timed out."}
]
```
"""


def test_parses_well_formed_block():
    children = parse_child_breakdown(WELL_FORMED)
    assert children == [
        {"index": 0, "goal": "write the intro", "model": "openrouter/free-model", "status": "ok", "result_excerpt": "Done, 3 paragraphs."},
        {"index": 1, "goal": "write the conclusion", "model": "openrouter/free-model", "status": "error", "result_excerpt": "Timed out."},
    ]


def test_no_block_returns_none():
    assert parse_child_breakdown("Just a plain final answer, no structured block.") is None


def test_empty_text_returns_none():
    assert parse_child_breakdown("") is None
    assert parse_child_breakdown(None) is None


def test_malformed_json_returns_none():
    text = "Final answer.\n```json\n[this is not valid json}\n```"
    assert parse_child_breakdown(text) is None


def test_non_list_json_returns_none():
    text = "Final answer.\n```json\n{\"not\": \"a list\"}\n```"
    assert parse_child_breakdown(text) is None


def test_uses_last_block_when_multiple_present():
    text = (
        "```json\n[{\"goal\": \"stale one\", \"status\": \"ok\"}]\n```\n"
        "more text\n"
        "```json\n[{\"goal\": \"the real one\", \"status\": \"ok\"}]\n```"
    )
    children = parse_child_breakdown(text)
    assert len(children) == 1
    assert children[0]["goal"] == "the real one"


def test_skips_entries_missing_a_goal_but_keeps_valid_ones():
    text = '```json\n[{"status": "ok"}, {"goal": "valid", "status": "ok"}]\n```'
    children = parse_child_breakdown(text)
    assert children == [{"index": 1, "goal": "valid", "model": "", "status": "ok", "result_excerpt": ""}]


def test_all_entries_invalid_returns_none():
    text = '```json\n[{"status": "ok"}, 42, "not a dict"]\n```'
    assert parse_child_breakdown(text) is None


def test_invalid_status_defaults_to_ok():
    text = '```json\n[{"goal": "x", "status": "weird"}]\n```'
    children = parse_child_breakdown(text)
    assert children[0]["status"] == "ok"


def test_result_excerpt_is_truncated():
    long_text = "x" * 900
    text = f'```json\n[{{"goal": "x", "result_excerpt": "{long_text}"}}]\n```'
    children = parse_child_breakdown(text)
    assert len(children[0]["result_excerpt"]) == 500
