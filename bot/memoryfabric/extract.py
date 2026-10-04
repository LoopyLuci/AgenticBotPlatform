"""Memories proposed from what the person says, whichever model is answering: only things stated outright ("remember
that ...", "my name is ...", "I prefer ...", "never ..."), never guesses. They go through the same review gate as
any other memory (bot/memory.py), so nothing becomes a memory unless the bot's gate is off or a person approves it.
"For every bot" / "everywhere" / "all models" makes it a shared memory."""
from __future__ import annotations

import re
from typing import Optional

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_SHARED = re.compile(r"\b(for|in|across) (every|all) (bots?|models?|agents?)\b|\beverywhere\b", re.I)
_RULES = [
    (re.compile(r"\b(?:please )?remember (?:that |this: ?)?(?P<x>.{6,300})", re.I), "fact", "{x}"),
    (re.compile(r"\bmy name is (?P<x>[A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?)", re.I), "user", "The person's name is {x}."),
    (re.compile(r"\bcall me (?P<x>[A-Z][\w'-]{1,30})", re.I), "user", "The person wants to be called {x}."),
    (re.compile(r"\bmy pronouns are (?P<x>[a-z]+/[a-z]+)", re.I), "user", "The person's pronouns are {x}."),
    (re.compile(r"\bi (?:always )?prefer (?P<x>.{4,200})", re.I), "feedback", "The person prefers {x}"),
    (re.compile(r"\b(?:please )?(?:never|don't ever|do not ever) (?P<x>.{4,200})", re.I), "feedback", "Never {x}"),
    (re.compile(r"\b(?:please )?always (?P<x>(?:use|run|reply|answer|write|ask|check|keep|put) .{3,200})", re.I),
     "feedback", "Always {x}"),
]


def candidates(text: str) -> list[dict]:
    """[{"content", "kind", "shared"}] for each statement worth remembering in `text`."""
    out = []
    for sentence in _SPLIT.split(text or ""):
        s = sentence.strip()
        if not s or s.endswith("?") or len(s) > 400:          # questions are not instructions
            continue
        for pattern, kind, template in _RULES:
            m = pattern.search(s)
            if m:
                x = re.sub(r"\s+", " ", _SHARED.sub("", m.group("x"))).strip(" ,;:-.!")
                if len(x) < 2:
                    continue
                content = template.format(x=x).strip()
                if not content.endswith((".", "!")):
                    content += "."
                out.append({"content": content[0].upper() + content[1:], "kind": kind, "shared": bool(_SHARED.search(s))})
                break
    return out


def propose(instance_id: Optional[int], text: str) -> list[dict]:
    """Record each candidate through the gate (pending unless the gate is off). Returns what was recorded."""
    from bot.memoryfabric import store
    if not store.settings()["auto_extract"]:
        return []
    done = []
    for c in candidates(text):
        r = store.remember(c["content"], instance_id=instance_id, shared=c["shared"], source="auto", kind=c["kind"])
        done.append({**c, **r})
    return done
