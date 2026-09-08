"""Context compression for the native agent loop — a length-based
heuristic (character count, not a real per-provider tokenizer; matches
bot/swarm_budget.py's own precedent of an honest heuristic over
exact-but-fragile per-provider token counting) that keeps a long-running
session's stored history from eventually blowing a model's context
window with no graceful degradation.

Checked once, at the very start of NativeAgentBackend.ask() (before this
turn's own new prompt is appended) — never in a loop, so this adds at
most one extra transport.send() call per turn, and only on the (rare)
turn that actually crosses the threshold. When it fires, everything
older than the most recent KEEP_LAST_N_MESSAGES turns is summarized by
one plain (no tools, no system prompt) call to the same model, and the
DB's stored history for that session is replaced with one synthetic
digest entry followed by the kept recent turns — see
bot.db.compress_agent_messages() for exactly how that replacement stays
chronologically correct.

Fails open: any error making the digest call (timeout, malformed
response, network failure) is logged and swallowed — an un-compressed,
slightly-too-long history is a strictly better outcome than losing the
turn entirely over a summarization call's own failure.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.agent_runtime.transports.base import ProviderTransport

logger = logging.getLogger("bot.agent_runtime.compression")

DEFAULT_THRESHOLD_CHARS = 60000
# A handful of the most recent turns always stay verbatim, never folded
# into the digest — keeps immediate conversational context (the last
# thing the user actually said) crisp rather than paraphrased.
KEEP_LAST_N_MESSAGES = 6
# The digest reply itself should stay short regardless of the backend's
# own configured max_tokens (which governs the real turn's replies, not
# this compaction summary).
DIGEST_MAX_TOKENS = 1024
DIGEST_TIMEOUT_S = 60.0

DIGEST_PROMPT = (
    "Summarize the conversation transcript below in a compact form. "
    "Preserve: concrete decisions made, open questions or threads, and "
    "any state (file paths, values, plans) a continuation would need. "
    "Do not include pleasantries, and do not restate these instructions.\n\n---\n\n"
)


def threshold_chars() -> int:
    from bot.config import config

    return config.current.get("native_agent", {}).get("compression_threshold_chars", DEFAULT_THRESHOLD_CHARS)


def _history_char_count(history: list[dict]) -> int:
    total = 0
    for entry in history:
        total += len(_flatten(entry.get("content")))
    return total


def _flatten(value: Any, _depth: int = 0) -> str:
    """Best-effort plain-text rendering of a stored-history `content`
    value, whatever shape the active transport happens to use (a plain
    string, an Anthropic content-block list, an OpenAI-compatible dict,
    a Responses API {"output": [...]} dict) — good enough for a
    summarization prompt, not a data-preserving serialization."""
    if _depth > 6 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output"):
            if value.get(key) is not None:
                return _flatten(value[key], _depth + 1)
        if value.get("name"):
            return f"[tool call: {value['name']}]"
        return ""
    return str(value)


def _render_transcript(history: list[dict]) -> str:
    return "\n".join(f"{entry.get('role', '?')}: {_flatten(entry.get('content'))}" for entry in history)


async def maybe_compress(session_key: str, transport: ProviderTransport, *, model: str) -> bool:
    """Returns whether it actually compressed anything this call."""
    from bot import db

    threshold = threshold_chars()
    if threshold <= 0:
        return False

    history = db.list_agent_messages(session_key)
    if len(history) <= KEEP_LAST_N_MESSAGES:
        return False
    if _history_char_count(history) <= threshold:
        return False

    to_summarize = history[:-KEEP_LAST_N_MESSAGES]
    transcript = _render_transcript(to_summarize)

    try:
        digest_request = transport.user_message(DIGEST_PROMPT + transcript)
        response = await transport.send(
            model=model, history=[digest_request], tool_schemas=[],
            max_tokens=DIGEST_MAX_TOKENS, timeout_s=DIGEST_TIMEOUT_S,
        )
    except Exception:
        logger.exception(
            "compression: digest call failed for session %s — leaving history uncompressed this turn", session_key
        )
        return False

    digest_text = response.text or "(summary unavailable)"
    digest_entry = transport.user_message(f"[Summary of earlier conversation]\n{digest_text}")
    db.compress_agent_messages(
        session_key, keep_last_n=KEEP_LAST_N_MESSAGES,
        digest_role=digest_entry["role"], digest_content=digest_entry["content"],
    )
    logger.info(
        "compression: session %s compressed %d older message(s) into one digest", session_key, len(to_summarize)
    )
    return True
