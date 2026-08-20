"""Known model choices per backend, for validation and dashboard dropdowns.

Only the `api` backend (direct Anthropic API calls) has a closed, known list
— the two Hermes backends (bot/backends/hermes_cli_backend.py,
hermes_gateway_backend.py) accept any non-empty string since this codebase
has no way to enumerate what a given Hermes CLI install actually supports.
"""

from __future__ import annotations

KNOWN_MODELS: dict[str, list[str]] = {
    "api": [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5-20251001",
    ],
}
