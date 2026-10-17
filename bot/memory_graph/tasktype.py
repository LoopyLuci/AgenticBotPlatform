"""A coarse task type from a goal: the key the graph's tool/skill edges hang on. Keyword rules, no model call."""
from __future__ import annotations

import re

_RULES: tuple[tuple[str, str], ...] = (
    ("fix_bug", r"\b(fix|bug|broken|crash|error|traceback|regression|failing|fails?)\b"),
    ("test", r"\b(tests?|pytest|coverage|unittest)\b"),
    ("refactor", r"\b(refactor|rename|clean ?up|restructure|simplif\w*)\b"),
    ("review", r"\b(review|audit|inspect)\b"),
    ("docs", r"\b(docs?|documentation|readme|changelog)\b"),
    ("research", r"\b(research|investigate|find out|explain|why|how does|summari[sz]e)\b"),
    ("build", r"\b(add|implement|create|build|write|new|feature|support)\b"),
    ("ops", r"\b(deploy|release|install|configure|config|server|service|docker|ci)\b"),
)


def classify(text: str) -> str:
    low = (text or "").lower()
    for name, pat in _RULES:
        if re.search(pat, low):
            return name
    return "general"
