"""Turning a stored agent conversation into something a person can read, attach to a bug report or share
(roadmap P5).

    render(messages, fmt="md")   Markdown transcript, or "json" for the normalised list
    export_session(key, fmt)     read a session from the database and render it

The stored messages are in whatever shape each transport uses (Anthropic content blocks, OpenAI messages with
tool_calls, plain strings); this flattens all of them to: who spoke, what they said, which tools were called
with what (shortened) and what came back (shortened). Anything that looks like a credential, and any secret
the server holds, is removed on the way out (secrets_guard.redact) - an export is meant to leave the machine.

There is no hosted "share link": ABP has no public server to host one. The export file is the thing to share.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from bot.agent_runtime import secrets_guard

MAX_TOOL_ARGS = 400
MAX_TOOL_RESULT = 1200
MAX_MESSAGES = 2000


def _short(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def _blocks_to_parts(content: Any) -> list[dict]:
    """One stored message -> [{"kind": "text"|"tool_call"|"tool_result"|"note", ...}]."""
    parts: list[dict] = []
    if content is None:
        return parts
    if isinstance(content, str):
        return [{"kind": "text", "text": content}] if content.strip() else []
    if isinstance(content, dict):                                       # OpenAI-style: {"content", "tool_calls"} or a tool result
        if "tool_call_id" in content:
            return [{"kind": "tool_result", "id": content.get("tool_call_id"), "text": _short(content.get("content", ""), MAX_TOOL_RESULT)}]
        if content.get("content"):
            parts += _blocks_to_parts(content["content"])
        for call in content.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments", call.get("arguments", ""))
            parts.append({"kind": "tool_call", "name": fn.get("name") or call.get("name"), "id": call.get("id"), "args": _short(args, MAX_TOOL_ARGS)})
        return parts
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append({"kind": "text", "text": block})
            elif not isinstance(block, dict):
                continue
            elif block.get("type") == "text":
                parts.append({"kind": "text", "text": str(block.get("text", ""))})
            elif block.get("type") == "tool_use":
                parts.append({"kind": "tool_call", "name": block.get("name"), "id": block.get("id"), "args": _short(block.get("input", {}), MAX_TOOL_ARGS)})
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(str(b.get("text", "")) for b in inner if isinstance(b, dict))
                parts.append({"kind": "tool_result", "id": block.get("tool_use_id"), "text": _short(inner or "", MAX_TOOL_RESULT)})
            elif block.get("type") in ("image", "image_url"):
                parts.append({"kind": "note", "text": "[an image was attached]"})
            elif block.get("type") in ("thinking", "redacted_thinking"):
                continue                                                # reasoning is not part of the record
            elif "name" in block and "id" in block:                     # the scripted transport's compact form
                parts.append({"kind": "tool_call", "name": block["name"], "id": block["id"], "args": ""})
    return parts


def normalise(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages[-MAX_MESSAGES:]:
        parts = _blocks_to_parts(m.get("content"))
        if parts:
            out.append({"role": m.get("role", "?"), "parts": parts})
    return out


# Things that look like credentials even if this server never held them (a key the user pasted into the chat).
_SHAPES = [(re.compile(p), r) for p, r in [
    (r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[private key removed]"),
    (r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}", "[key removed]"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "[token removed]"),
    (r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", "[token removed]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[key removed]"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "[key removed]"),
    (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}", "Bearer [token removed]"),
    (r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\\?\"?\s*[:=]\s*\\?\"?)[^\s\\\"',;]{6,}", r"\1\2[removed]"),
]]


def _clean(text: str) -> str:
    for rx, repl in _SHAPES:                  # first: the patterns would otherwise mangle the [secret:NAME] markers below
        text = rx.sub(repl, text)
    return secrets_guard.redact(text)


def render(messages: list[dict], fmt: str = "md", *, title: str = "", meta: Optional[dict] = None) -> str:
    """`messages` as stored (oldest first). fmt is "md" or "json". The result has secrets removed."""
    if fmt not in ("md", "json"):
        raise ValueError("format must be md or json")
    turns = normalise(messages)
    if fmt == "json":
        body = json.dumps({"title": title, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **(meta or {}),
                           "messages": turns}, indent=1, ensure_ascii=False)
        return _clean(body)
    lines = [f"# {title or 'Agent conversation'}", ""]
    for k, v in (meta or {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", f"_Exported {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Tool output is shortened; secrets are removed._", ""]
    for turn in turns:
        who = {"user": "You", "assistant": "Agent"}.get(turn["role"], turn["role"].title())
        lines.append(f"## {who}")
        for part in turn["parts"]:
            if part["kind"] == "text":
                lines += ["", part["text"].strip()]
            elif part["kind"] == "tool_call":
                lines += ["", f"> tool: `{part['name']}` {('`' + part['args'].replace(chr(96), chr(39)) + '`') if part['args'] else ''}".rstrip()]
            elif part["kind"] == "tool_result":
                shown = part["text"].strip().replace("\n", "\n> ")
                lines += ["", f"> result: {shown}" if shown else "> result: (empty)"]
            else:
                lines += ["", f"_{part['text']}_"]
        lines.append("")
    return _clean("\n".join(lines).rstrip() + "\n")


def export_session(session_key: str, fmt: str = "md", *, title: str = "") -> str:
    from bot import db

    messages = db.list_agent_messages(session_key, limit=MAX_MESSAGES)
    if not messages:
        raise LookupError("that session has no messages (or does not exist)")
    return render(messages, fmt, title=title or "Agent conversation", meta={"messages": len(messages)})
