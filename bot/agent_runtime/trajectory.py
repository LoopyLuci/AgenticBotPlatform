"""Turning finished agent runs into training and evaluation data (roadmap P8).

    python -m abp_trajectory export --out runs.jsonl --confirm [--only-ok] [--min-tool-calls 1] [--scrub-pii]

Each line is one run as a chat transcript a fine-tuning or evaluation pipeline can read:

    {"id": run id, "model": ..., "outcome": "ok", "tokens": N, "iterations": n, "tools": {"read_file": 2},
     "messages": [{"role": "user", "content": "..."},
                  {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "arguments"}]},
                  {"role": "tool", "tool_call_id": "...", "name": "read_file", "content": "..."}, ...]}

The shape of a run (model, outcome, tools used, tokens) comes from the trace store; the words come from the session's stored
messages, which the trace deliberately does not copy. **The words are conversation content - a person's messages, their files'
contents, tool output.** So this is an explicit, opt-in action: nothing exports itself, the command refuses without
`--confirm`, and what leaves has secrets and credential-shaped strings removed (session_export.py's patterns), tool output
shortened, and, with `--scrub-pii`, e-mail addresses, phone numbers and IP addresses masked. Reading a session that other
people took part in is not something to do without their say-so; that judgement is the operator's.

Runs are skipped unless they finished (status ok), had no denied tool calls with `--exclude-denied`, and used at least
`--min-tool-calls`. Identical transcripts are written once. The output is only as good as those filters: nothing here scores
whether an answer was *right*.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Optional

from bot.agent_runtime import session_export, trace

MAX_TOOL_CHARS_DEFAULT = 2000
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,16}\d(?![\w.])")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scrub_pii(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _IPV4.sub("[ip]", text)
    return _PHONE.sub("[phone]", text)


def to_chat(messages: list[dict], *, max_tool_chars: int = MAX_TOOL_CHARS_DEFAULT, pii: bool = False) -> list[dict]:
    """Stored messages (any transport's shape) -> generic chat messages with tool calls and tool results."""
    clean = (lambda t: scrub_pii(session_export._clean(t))) if pii else session_export._clean
    out: list[dict] = []
    names: dict[str, str] = {}
    for turn in session_export.normalise(messages):
        role = turn["role"]
        texts = [p["text"] for p in turn["parts"] if p["kind"] == "text"]
        calls = [p for p in turn["parts"] if p["kind"] == "tool_call"]
        results = [p for p in turn["parts"] if p["kind"] == "tool_result"]
        if role == "assistant" or (role == "user" and not results):
            msg: dict = {"role": "assistant" if role == "assistant" else "user", "content": clean("\n".join(texts))}
            if calls:
                msg["tool_calls"] = []
                for c in calls:
                    names[str(c["id"])] = str(c["name"])
                    msg["tool_calls"].append({"id": c["id"], "name": c["name"], "arguments": clean(c["args"])})
            if msg["content"] or msg.get("tool_calls"):
                out.append(msg)
        for r in results:
            text = clean(r["text"])
            out.append({"role": "tool", "tool_call_id": r["id"], "name": names.get(str(r["id"]), ""),
                        "content": text if len(text) <= max_tool_chars else text[:max_tool_chars] + " [shortened]"})
        if role == "tool" and not results:
            for t in texts:
                out.append({"role": "tool", "content": clean(t)[:max_tool_chars]})
    return out


def runs(*, only_ok: bool = True, min_tool_calls: int = 0, exclude_denied: bool = False, models: Optional[Iterable[str]] = None,
         limit: int = 1000, store=None) -> list[dict]:
    """Finished top-level runs (not sub-agents) with their summaries, oldest first."""
    store = store or trace.get_store()
    wanted = {m for m in (models or [])}
    out = []
    for ev in store.events(kind="agent.run.start", limit=limit):
        data = ev.get("data") or {}
        if data.get("parent_run") or not data.get("session"):
            continue
        summary = trace.summarize(ev["run_id"], store)
        if only_ok and summary["status"] != "ok":
            continue
        if exclude_denied and summary["denied"]:
            continue
        if len(summary["tool_calls"]) < min_tool_calls:
            continue
        if wanted and data.get("model") not in wanted:
            continue
        out.append({**summary, "model": data.get("model", ""), "session": data["session"]})
    return out


def export(*, only_ok: bool = True, min_tool_calls: int = 0, exclude_denied: bool = False, models: Optional[Iterable[str]] = None,
           max_tool_chars: int = MAX_TOOL_CHARS_DEFAULT, pii: bool = False, limit: int = 1000, store=None) -> list[dict]:
    """The exportable records. A session with several runs is exported once, up to its last selected run."""
    from bot import db

    records, seen_sessions, seen_hashes = [], set(), set()
    selected = runs(only_ok=only_ok, min_tool_calls=min_tool_calls, exclude_denied=exclude_denied, models=models, limit=limit, store=store)
    for r in reversed(selected):                                  # newest run of a session first: it holds the whole conversation
        if r["session"] in seen_sessions:
            continue
        seen_sessions.add(r["session"])
        messages = to_chat(db.list_agent_messages(r["session"]), max_tool_chars=max_tool_chars, pii=pii)
        if not any(m["role"] == "assistant" for m in messages):
            continue
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        records.append({"id": r["run_id"], "model": r["model"], "outcome": r["status"], "tokens": r["tokens"], "iterations": r["iterations"],
                        "tools": r["tool_counts"], "denied": r["denied"], "messages": messages})
    records.reverse()
    return records


def write_jsonl(records: list[dict], path) -> int:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)
