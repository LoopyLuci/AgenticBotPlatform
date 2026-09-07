"""Canonical "how hard should the model think" vocabulary, shared across
every backend BotServer talks to.

Three real, different vocabularies exist for this same underlying concept:
- Claude Desktop's own UI slider (bot/backends/ui_backend.py's EFFORT_LEVELS):
  low/medium/high/extra/max/ultracode.
- Anthropic's real Messages API (confirmed live against platform.claude.com,
  matching this deployment's actual model family — claude-sonnet-5,
  claude-opus-5, the Fable/Mythos 5.1 family): a top-level
  output_config.effort field accepting low/medium/high/xhigh/max, no
  "off"/"none" level, defaulting to "high" when omitted.
- Hermes Agent's own real, already-built reasoning_effort ladder (confirmed
  by reading its installed source, agent/reasoning_effort.py):
  none/minimal/low/medium/high/xhigh/max/ultra, configurable globally
  (agent.reasoning_effort), per-model (agent.reasoning_overrides), and
  per-delegation/child-agent (delegation.reasoning_effort).

This module picks Hermes's real 8-level ladder as BotServer's own canonical
vocabulary (it's the most granular and already has genuine prior art, not
invented for this project) and provides honest, explicit downward mappings
to whatever a given backend actually supports — never inventing a
capability a backend doesn't have.
"""

from __future__ import annotations

from typing import Optional

# Mirrors Hermes Agent's own real EFFORT_LADDER constant (agent/reasoning_effort.py)
# — same name and order, credited here rather than re-invented.
EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

DEFAULT_EFFORT = "high"


def is_valid(level: Optional[str]) -> bool:
    return level in EFFORT_LADDER


# Anthropic's real output_config.effort has no "off" level and nothing above
# "max" — none/minimal collapse to omitting the field entirely (letting the
# API's own "high" default apply), and max/ultra both collapse to "max"
# since there is nothing higher to ask for.
_TO_ANTHROPIC: dict[str, Optional[str]] = {
    "none": None,
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}


def to_anthropic(level: Optional[str]) -> Optional[str]:
    """The value to send as output_config.effort, or None to omit the field
    entirely (letting the API's own default apply) — never guesses a value
    for an unrecognized level."""
    if level is None:
        return None
    return _TO_ANTHROPIC.get(level)


# ui_backend.py's own EFFORT_LEVELS keys, mapped onto by the canonical
# ladder's closest real equivalent. That module owns the actual slider
# automation; this is only the translation a caller uses before calling
# into it.
_TO_UI_BACKEND: dict[str, str] = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "extra",
    "max": "max",
    "ultra": "ultracode",
}


def to_ui_backend(level: Optional[str]) -> str:
    """Always returns a real ui_backend.py EFFORT_LEVELS key — falls back
    to that module's own DEFAULT_EFFORT for an unrecognized/missing level
    rather than raising, since the ui backend must always have some value
    to set the slider to."""
    from bot.backends.ui_backend import DEFAULT_EFFORT as UI_DEFAULT_EFFORT

    if level is None:
        return UI_DEFAULT_EFFORT
    return _TO_UI_BACKEND.get(level, UI_DEFAULT_EFFORT)


# Best-effort only: OpenAI's own documented reasoning_effort enum (and
# what several OpenRouter-routed models accept passthrough) is the
# 3-value low/medium/high — not the full 8-level ladder. Collapsing
# anything above "high" down to "high" (rather than sending an
# unrecognized enum value like "xhigh"/"ultra" verbatim) avoids risking a
# real 400 from a provider that DOES strictly validate this field, unlike
# an entirely unknown field name, which most providers simply ignore.
_TO_OPENAI_REASONING_EFFORT: dict[str, Optional[str]] = {
    "none": None,
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
    "ultra": "high",
}


def to_openai_reasoning_effort(level: Optional[str]) -> Optional[str]:
    """The value to send as an OpenAI-compatible "reasoning_effort" field,
    or None to omit it (provider default applies) — never sends a value
    outside OpenAI's own real low/medium/high enum, since several
    real endpoints validate it strictly."""
    if level is None:
        return None
    return _TO_OPENAI_REASONING_EFFORT.get(level)


def to_hermes(level: Optional[str]) -> Optional[str]:
    """Identity mapping — Hermes's own vocabulary IS the canonical one.
    Returns None (meaning "don't set this key", i.e. inherit) for an
    unrecognized/missing level rather than writing a bogus value into
    Hermes's real config.yaml."""
    if level is None or level not in EFFORT_LADDER:
        return None
    return level
