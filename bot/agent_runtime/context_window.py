"""How full the conversation is, and what to do about it (roadmap P3).

**Counting.** There is no offline tokenizer for every provider, so tokens are
estimated from characters and the estimate is *calibrated against the provider's own
count*: after each response the number of input tokens the provider reports is compared
with the characters that were sent, and a per-model chars-per-token ratio is updated
(exponential average). The estimate starts at 3.5 characters per token and converges
on the real ratio for the text this model is actually seeing.

**Windows.** `window_for(model)` knows the common families (a table, newest match wins)
and can be overridden per model under `native_agent.context_windows`. Unknown models get
128 000, a middling default; set the real figure for a model you rely on.

**Managing the fill** (`manage`), before every model call in a turn:

1. below the threshold (default 70 % of the window) nothing happens;
2. above it, old tool outputs are cleared - replaced by a one-line note saying how big
   they were - keeping the most recent ones intact. Cheap, no model call. The stored
   conversation is untouched; only what is sent is shortened;
3. still above it, the caller is told to summarise the older conversation (the
   existing compaction), which does replace stored messages with a digest.

    native_agent:
      context:
        compact_at: 0.70        # fraction of the window at which to start
        keep_tool_results: 6    # most recent tool outputs never cleared
      context_windows:
        my-local-model: 32768
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_WINDOW = 128_000
DEFAULT_RATIO = 3.5
MIN_RATIO, MAX_RATIO = 1.5, 8.0
EMA = 0.3
PER_MESSAGE_OVERHEAD = 4
DEFAULT_COMPACT_AT = 0.70
DEFAULT_KEEP_TOOL_RESULTS = 6

# (pattern, window) - first match wins, so put the most specific first.
_WINDOWS: list[tuple[re.Pattern, int]] = [(re.compile(p, re.I), n) for p, n in [
    (r"claude-(opus|sonnet)-(4|5)", 200_000), (r"claude-haiku", 200_000), (r"claude", 200_000),
    (r"gpt-4\.1", 1_000_000), (r"gpt-5", 400_000), (r"gpt-4o|gpt-4-turbo|o[134](-|$)", 128_000), (r"gpt-4", 8_192),
    (r"gpt-3\.5", 16_385), (r"gemini-(1\.5|2|3)", 1_000_000), (r"gemini", 32_768),
    (r"llama-?3\.[123]|llama3\.[123]", 128_000), (r"llama", 8_192), (r"mistral-large|mixtral-8x22", 128_000),
    (r"mistral|mixtral", 32_768), (r"qwen.*(2\.5|3)", 131_072), (r"deepseek", 128_000), (r"grok", 131_072),
    (r"gemma", 8_192), (r"phi-?3", 128_000),
]]

_ratios: dict[str, float] = {}


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("native_agent") or {})
    except Exception:  # noqa: BLE001
        return {}


def window_for(model: str) -> int:
    overrides = _cfg().get("context_windows") or {}
    name = str(model or "")
    for key, value in overrides.items():
        if key == name or name.startswith(str(key)):
            try:
                return max(1024, int(value))
            except (TypeError, ValueError):
                continue
    base = name.rsplit("/", 1)[-1]
    for rx, n in _WINDOWS:
        if rx.search(base):
            return n
    return DEFAULT_WINDOW


def ratio_for(model: str) -> float:
    return _ratios.get(str(model), DEFAULT_RATIO)


def observe(model: str, chars_sent: int, input_tokens: Optional[int]) -> None:
    """Learn from the provider's own count of what it was sent."""
    if not input_tokens or input_tokens < 50 or chars_sent < 200:
        return
    seen = min(MAX_RATIO, max(MIN_RATIO, chars_sent / input_tokens))
    key = str(model)
    _ratios[key] = seen if key not in _ratios else (1 - EMA) * _ratios[key] + EMA * seen


def forget_calibration() -> None:
    _ratios.clear()


# ---- measuring ------------------------------------------------------------------
def _entry_chars(value: Any, depth: int = 0) -> int:
    if depth > 6 or value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (int, float, bool)):
        return len(str(value))
    if isinstance(value, list):
        return sum(_entry_chars(v, depth + 1) for v in value)
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url"):
            return 4000                                    # an image costs roughly a thousand tokens whatever its bytes
        return sum(_entry_chars(v, depth + 1) for k, v in value.items() if k not in ("signature", "cache_control"))
    return len(str(value))


def measure_chars(history: list[dict], system_prompt: Optional[str] = None, tool_schemas: Optional[list] = None) -> int:
    chars = _entry_chars(system_prompt or "")
    if tool_schemas:
        chars += len(json.dumps(tool_schemas, default=str))
    for entry in history:
        chars += _entry_chars(entry.get("content")) + PER_MESSAGE_OVERHEAD * 4
    return chars


def estimate_tokens(history: list[dict], model: str, system_prompt: Optional[str] = None,
                    tool_schemas: Optional[list] = None) -> int:
    return int(measure_chars(history, system_prompt, tool_schemas) / ratio_for(model))


# ---- managing -------------------------------------------------------------------
@dataclass
class Report:
    history: list[dict]                       # what to send (possibly with old tool outputs cleared)
    window: int = 0
    before: int = 0                           # estimated tokens before any clearing
    after: int = 0                            # estimated tokens after clearing
    cleared: int = 0                          # tool outputs replaced by a note
    needs_summary: bool = False               # still too full: summarise the older conversation
    notes: list[str] = field(default_factory=list)


def manage(history: list[dict], transport, model: str, system_prompt: Optional[str] = None,
           tool_schemas: Optional[list] = None) -> Report:
    cfg = (_cfg().get("context") or {})
    try:
        compact_at = float(cfg.get("compact_at", DEFAULT_COMPACT_AT))
        keep = int(cfg.get("keep_tool_results", DEFAULT_KEEP_TOOL_RESULTS))
    except (TypeError, ValueError):
        compact_at, keep = DEFAULT_COMPACT_AT, DEFAULT_KEEP_TOOL_RESULTS
    window = window_for(model)
    before = estimate_tokens(history, model, system_prompt, tool_schemas)
    report = Report(history=history, window=window, before=before, after=before)
    if compact_at <= 0 or before <= window * compact_at:
        return report
    pruned, cleared = transport.prune_tool_results(history, keep) if hasattr(transport, "prune_tool_results") else (history, 0)
    report.history, report.cleared = pruned, cleared
    report.after = estimate_tokens(pruned, model, system_prompt, tool_schemas)
    if cleared:
        report.notes.append(f"cleared {cleared} old tool output(s): about {before - report.after} tokens")
    report.needs_summary = report.after > window * compact_at
    return report


def placeholder(chars: int, preview: str = "") -> str:
    preview = " ".join(preview.split())[:100]
    shown = f' It began: "{preview}".' if preview else ""
    return f"[Output cleared to save space ({chars} characters).{shown} Run the tool again if you need it.]"
