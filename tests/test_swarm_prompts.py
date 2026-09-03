"""bot/swarm/prompts.py's hermes_delegation_goal — Phase 9 extended this
with an instruction to emit a structured ```json per-child breakdown at
the end of the reply (parsed back out by bot/swarm/child_parser.py)."""

from __future__ import annotations

from bot.swarm.prompts import hermes_delegation_goal


def test_includes_goal_text():
    prompt = hermes_delegation_goal("build the widget")
    assert "build the widget" in prompt


def test_includes_structured_breakdown_instruction():
    prompt = hermes_delegation_goal("do it")
    assert "```json" in prompt
    assert "result_excerpt" in prompt


def test_routing_note_mentions_explicit_worker_model():
    prompt = hermes_delegation_goal("do it", worker_provider="openrouter", worker_model="free-model")
    assert "openrouter/free-model" in prompt


def test_concurrency_note_mentions_max_children():
    prompt = hermes_delegation_goal("do it", max_children=4)
    assert "4 subtasks in parallel" in prompt
