"""The turn's tool pool, and `find_tools`: every tool a turn may use stays reachable even when only a few are sent.

A small local model is sent a compact core set (small_model.py) so its prompt stays short, which used to put skills,
memory, code search, web, generation tools, modules and MCP servers out of its reach for good. Now each native turn
records its pool - every tool it is permitted (after the admin gate and the caller's allowed_tools, before the
small-model trim) - and `find_tools` searches that pool and adds what it finds to the tools sent from the next step on.

The pool is also enforced: a tool call naming a tool outside it is refused before it runs (tool_loop._run_one_tool).
Before this, allowed_tools only hid schemas, so a model that named a hidden tool (a leaf sub-agent naming
spawn_subagent, a Review turn naming write_file) still had it run. A tool call made outside a native turn (no pool
recorded) is not affected.

    begin() / end(token)        a holder per turn (native_backend._ask_traced); a sub-agent's turn gets its own
    attach(pool, sent)          the permitted schemas and the list that is sent (find_tools adds to it in place)
    permitted(name)             False only when a pool is recorded and the name is not in it
    search(query, limit)        the pool's tools ranked for a plain-words query
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Optional

TOOL_NAME = "find_tools"
MAX_RESULTS = 8
# Added to the system prompt when the sent tools are a trimmed set: a small model tends to reach for run_shell instead.
PROMPT_HINT = ("\n\nYou have many more tools than the ones listed (skills, memory and notes, code search, web, image and "
               "asset generation, models, ABP modules and more). Before using run_shell to find or inspect something ABP "
               "knows about, call find_tools with what you want to do; the tools it returns can be called right away.")


class _Turn:
    __slots__ = ("pool", "sent")

    def __init__(self) -> None:
        self.pool: Optional[dict] = None     # name -> schema; None until attach()
        self.sent: Optional[list] = None     # the list of schemas the turn sends (shared with the backend)


_current: "contextvars.ContextVar[Optional[_Turn]]" = contextvars.ContextVar("tool_discovery_turn", default=None)


def begin() -> contextvars.Token:
    return _current.set(_Turn())


def end(token: contextvars.Token) -> None:
    _current.reset(token)


def attach(pool: list, sent: list) -> None:
    turn = _current.get()
    if turn is None:
        return
    turn.pool = {s["name"]: s for s in pool}
    turn.sent = sent


def permitted(name: str) -> bool:
    turn = _current.get()
    return turn is None or turn.pool is None or name in turn.pool


_STOP = frozenset("""a an and are as at be by can do does for from get give has have how i in into is it its me my now of on
or our please right set show so tell than that the their them then there these this to up us use was we what when where which
who why will with you your about all any just like list make need see some via""".split())


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _words(text: str) -> list[str]:
    return [_stem(w) for w in re.split(r"[^a-z0-9]+", str(text).lower()) if len(w) > 1 and w not in _STOP]


# Plain words people use, mapped to words tool names and descriptions use.
_SYNONYMS = {
    "rag": ("memory", "recall", "context", "search", "vault"), "remember": ("memory", "save"), "recall": ("memory", "context"),
    "image": ("vision", "asset", "generate"), "picture": ("vision", "image"), "photo": ("vision", "image"),
    "asset": ("generate", "toolkit"), "generate": ("generate", "asset"), "web": ("fetch", "url"), "internet": ("fetch", "web"),
    "url": ("fetch",), "skill": ("skill",), "skills": ("skill",), "test": ("tests", "run"), "tests": ("run",),
    "symbol": ("find", "outline", "references"), "code": ("code", "symbol", "outline"), "train": ("train", "lab"),
    "model": ("model", "models"), "models": ("model",), "plugin": ("plugin",), "schedule": ("schedule",),
    "subagent": ("subagent", "spawn"), "agent": ("subagent", "agents"), "kanban": ("kanban", "card"),
}


def _scored(query: str) -> list[tuple[int, int, str, dict]]:
    """(score, how many query words the tool's NAME holds, name, schema) for every pool tool that matches, best first."""
    turn = _current.get()
    if turn is None or not turn.pool:
        return []
    want = _words(query)
    expanded = set(want)
    for w in want:
        expanded.update(_stem(s) for s in _SYNONYMS.get(w, ()))
    if not expanded:
        return []
    scored = []
    for name, schema in turn.pool.items():
        if name == TOOL_NAME:
            continue
        name_words = set(_words(name.replace("_", " ")))
        desc_words = set(_words(schema.get("description", "")))
        in_name = len(expanded & name_words)
        score = 3 * in_name + len(expanded & desc_words)
        if any(w in name for w in want if len(w) > 3):
            score += 2
        if score:
            scored.append((score, in_name, name, schema))
    scored.sort(key=lambda t: (-t[0], t[2]))
    return scored


def search(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """The pool's tools that best match `query`, best first; [] without a pool."""
    return [s for _, _, _, s in _scored(query)[:max(1, limit)]]


PREFETCH = 5


def prefetch(message: str, limit: int = PREFETCH) -> list[str]:
    """Before the first step: the pool's tools whose NAMES match the person's message are sent with the core set, so the
    turn can use them without first thinking to call find_tools (the coder on 2026-10-20 reached for grep and run_shell
    instead in 4 of 10 tasks). Name matches only: a whole message shares common words with almost every description."""
    turn = _current.get()
    if turn is None or turn.sent is None:
        return []
    have = {s["name"] for s in turn.sent}
    added = []
    for _, in_name, name, schema in _scored(message):
        if len(added) >= limit:
            break
        if in_name and name not in have:
            turn.sent.append(schema)
            have.add(name)
            added.append(name)
    return added


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def _find(tool_input: dict, **_context) -> str:
    inp = tool_input if isinstance(tool_input, dict) else {}
    query = str(inp.get("query") or "").strip()
    try:
        limit = max(1, min(int(inp.get("limit") or MAX_RESULTS), 15))
    except (TypeError, ValueError):
        limit = MAX_RESULTS
    turn = _current.get()
    if turn is None or turn.pool is None:
        return json.dumps({"tools": [], "note": "No tool pool in this turn: every tool you can use is already listed."})
    found = search(query, limit)
    added = []
    if turn.sent is not None:
        have = {s["name"] for s in turn.sent}
        for schema in found:
            if schema["name"] not in have:
                turn.sent.append(schema)
                have.add(schema["name"])
                added.append(schema["name"])
    rows = [{"name": s["name"], "description": _short(s.get("description", ""))} for s in found]
    note = ("These tools are now available: call them directly." if found else
            "Nothing matched. Try other words (what you want done, not a tool name).")
    return json.dumps({"tools": rows, "added": added, "note": note})


SCHEMA = {
    "name": TOOL_NAME,
    "description": "Find and load more tools. You can use far more tools than are listed now: skills, memory and RAG recall, "
                   "code search and symbol navigation, web fetch, image and asset generation, vision, model management, ABP "
                   "modules and MCP servers, sub-agents, schedules, plugins and more. Describe what you want to do; the "
                   "matching tools are added and you can call them on your next step.",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "what you want to do, in plain words (e.g. 'search my notes', 'make an icon')"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 15}}, "required": ["query"]},
}


def register_all() -> None:
    from bot.agent_runtime import toolspec

    toolspec.register(SCHEMA, toolspec.ToolSpec(TOOL_NAME, "read", read_only=True, concurrency_safe=False, origin="registered"),
                      _find)


register_all()
