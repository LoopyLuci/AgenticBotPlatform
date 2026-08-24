"""Persona presets for bot instances — a small, curated set of role
templates (icon + label + default system-prompt instructions) that a bot
instance can be assigned, purely to make the Bots GUI's picker meaningful
and to seed custom_instructions with a sensible starting point. Nothing
here is enforced by the backend layer: a persona is metadata plus a
default for custom_instructions, not a distinct code path.
"""

from __future__ import annotations

from typing import Any

PERSONA_PRESETS: dict[str, dict[str, Any]] = {
    "assistant": {
        "label": "Assistant",
        "icon": "💬",
        "description": "General-purpose helper for whoever messages this bot.",
        "instructions": "",
    },
    "coder": {
        "label": "Coder",
        "icon": "🧑‍💻",
        "description": "Writes and reviews code, favors concrete diffs over discussion.",
        "instructions": (
            "You are acting as a software engineer. Prefer concrete code and diffs over "
            "discussion, call out risks or missing tests, and keep explanations brief."
        ),
    },
    "designer": {
        "label": "Designer",
        "icon": "🎨",
        "description": "Focuses on UX/visual design decisions and rationale.",
        "instructions": (
            "You are acting as a product/UX designer. Focus on user experience, visual "
            "clarity, and consistency; explain the reasoning behind design choices."
        ),
    },
    "manager": {
        "label": "Manager",
        "icon": "🧭",
        "description": "Coordinates and delegates to the bots it manages rather than doing the work itself.",
        "instructions": (
            "You are acting as a manager coordinating other bots. Break incoming requests "
            "into clear delegated tasks, track what each assistant reports back, and "
            "summarize the combined outcome rather than doing the detailed work yourself."
        ),
    },
    "custom": {
        "label": "Custom",
        "icon": "⚙️",
        "description": "Write your own instructions from scratch.",
        "instructions": "",
    },
}

DEFAULT_PERSONA = "assistant"


def list_personas() -> list[dict[str, Any]]:
    return [{"id": key, **preset} for key, preset in PERSONA_PRESETS.items()]


def is_known(persona: str) -> bool:
    return persona in PERSONA_PRESETS
