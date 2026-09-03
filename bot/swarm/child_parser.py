"""Parses the structured per-child breakdown a dispatch_swarm_goal reply
is asked to include (see bot/swarm/prompts.py's hermes_delegation_goal)
into rows for bot.db.set_job_children().

This is the "post-hoc" half of Phase 9's hybrid swarm-observability
design — a Hermes gateway's own delegate_task children never reach
BotServer individually (see bot/swarm/observability.py's docstring for
why), so the only per-child detail available is what the orchestrator
chooses to report back in its own final reply. Parsing that is
inherently best-effort: a missing or malformed block must never break
the dispatch itself, only degrade to "no breakdown available" — the
same as today's behavior before this feature existed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_RESULT_EXCERPT_MAX = 500
_VALID_STATUSES = {"ok", "error"}


def parse_child_breakdown(final_text: str) -> Optional[list[dict[str, Any]]]:
    """Extracts the LAST fenced ```json block in final_text and parses it
    as a list of {"index", "goal", "model", "status", "result_excerpt"}
    entries. Returns None (never raises) if no block is found or nothing
    in it parses as a valid entry — the caller treats None exactly like
    "no breakdown available", not an error."""
    if not final_text:
        return None
    matches = _JSON_FENCE_RE.findall(final_text)
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None

    children: list[dict[str, Any]] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            continue
        goal = entry.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            continue
        status = entry.get("status") if entry.get("status") in _VALID_STATUSES else "ok"
        result_excerpt = entry.get("result_excerpt")
        if not isinstance(result_excerpt, str):
            result_excerpt = ""
        model = entry.get("model")
        if not isinstance(model, str):
            model = ""
        index = entry.get("index")
        if not isinstance(index, int):
            index = i
        children.append(
            {
                "index": index,
                "goal": goal.strip(),
                "model": model,
                "status": status,
                "result_excerpt": result_excerpt[:_RESULT_EXCERPT_MAX],
            }
        )
    return children or None
