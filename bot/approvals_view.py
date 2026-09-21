"""Approvals as things a person can look at and decide from anywhere (roadmap P6).

Every tool call that needs a person creates a row in `pending_approvals` (agent_runtime/approval.py). This module
turns a row into something reviewable - what will happen, with a diff for edits - and lists them across bot
instances, so the dashboard, the desktop app or a phone can show "the agent wants to: ..." with the change in front of
the person, not just a tool name. Deciding goes through `approval.resolve`, the same call the chat buttons use, so an
approval from any surface wakes the same waiting turn and is recorded the same way.

The preview is built from the tool's input alone (no file is read), shortened, and passed through the secret
redactor: a person deciding on a phone should see what will happen, not a credential.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Optional

from bot import db
from bot.agent_runtime import secrets_guard

MAX_PREVIEW = 3000
OUTCOMES = ("once", "session", "always", "deny")


def _clip(text: str, limit: int = MAX_PREVIEW) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters]"


def _diff(old: str, new: str, label: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{label}", f"b/{label}", lineterm="", n=2))
    return "\n".join(lines) if lines else "(no visible change)"


def preview(tool: str, tool_input: dict) -> dict:
    """{"summary": one line, "kind": "diff"|"command"|"text"|"json", "body": text}."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    path = str(inp.get("path") or "")
    if tool == "edit_file":
        old, new = str(inp.get("old_string") or ""), str(inp.get("new_string") or "")
        summary = f"Change {path}" + (" (every occurrence)" if inp.get("replace_all") else "")
        return {"summary": summary, "kind": "diff", "body": _clip(_diff(old, new, path) if old else f"+ (new file)\n{new}")}
    if tool == "multi_edit":
        parts = [_diff(str(e.get("old_string") or ""), str(e.get("new_string") or ""), path) for e in (inp.get("edits") or []) if isinstance(e, dict)]
        return {"summary": f"Make {len(parts)} edits to {path}", "kind": "diff", "body": _clip("\n\n".join(parts))}
    if tool == "write_file":
        content = str(inp.get("content") or "")
        return {"summary": f"Write {path} ({len(content):,} characters, replacing anything there)", "kind": "text", "body": _clip(content)}
    if tool == "apply_patch":
        patch = str(inp.get("patch") or "")
        files = sorted({ln[4:].split("\t")[0].removeprefix("b/") for ln in patch.splitlines() if ln.startswith("+++ ")})
        return {"summary": "Apply a patch to " + (", ".join(files[:5]) or "files"), "kind": "diff", "body": _clip(patch)}
    if tool in ("run_shell",):
        return {"summary": "Run a command", "kind": "command", "body": _clip(str(inp.get("command") or ""), 1500)}
    if tool == "browser_act":
        return {"summary": f"Browser: {inp.get('action')} on element {inp.get('ref', '')}".strip(), "kind": "json",
                "body": json.dumps({k: v for k, v in inp.items() if k != "text"} | ({"text": "(typed text)"} if inp.get("text") else {}), indent=1)}
    if tool == "browser_handoff":
        return {"summary": "The agent needs you to do something in the browser", "kind": "text", "body": str(inp.get("reason") or "")}
    if tool == "spawn_subagent":
        n = len(inp.get("tasks") or [])
        return {"summary": f"Start {n or 'a'} sub-agent task(s)", "kind": "json", "body": _clip(json.dumps(inp, indent=1, default=str), 1500)}
    return {"summary": f"Use {tool}", "kind": "json", "body": _clip(json.dumps(inp, indent=1, default=str), 1500)}


def describe(row) -> dict:
    """One approvals row as a reviewable object."""
    try:
        tool_input = json.loads(row["tool_input"])
    except (TypeError, ValueError):
        tool_input = {}
    p = preview(row["tool_name"], tool_input)
    out = {"id": row["id"], "instance_id": row["instance_id"], "chat_id": row["chat_id"], "session": row["session_key"], "tool": row["tool_name"],
           "status": row["status"], "created_at": row["created_at"], "resolved_at": row["resolved_at"], "resolved_by": row["resolved_by"],
           "summary": p["summary"], "kind": p["kind"], "body": p["body"]}
    for key in ("summary", "body"):
        out[key] = secrets_guard.redact(out[key])
    try:
        from bot.agent_runtime import taint

        out["untrusted_content_in_session"] = taint.is_tainted(row["session_key"])
    except Exception:  # noqa: BLE001
        out["untrusted_content_in_session"] = False
    return out


def listing(*, status: str = "pending", instance_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    where, args = [], []
    if status != "all":
        where.append("status=?")
        args.append(status)
    if instance_id is not None:
        where.append("instance_id=?")
        args.append(instance_id)
    sql = "SELECT * FROM pending_approvals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    rows = db.get_conn().execute(sql, (*args, max(1, min(limit, 200)))).fetchall()
    return [describe(r) for r in rows]


def get(approval_id: int) -> Optional[dict]:
    row = db.get_pending_approval(approval_id)
    return describe(row) if row else None


def decide(approval_id: int, outcome: str, actor: str) -> str:
    """"decided", "already_resolved" or "not_found". `outcome` is once, session, always or deny."""
    from bot.agent_runtime import approval

    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    row = db.get_pending_approval(approval_id)
    if row is None:
        return "not_found"
    if row["status"] != "pending":
        return "already_resolved"
    return "decided" if approval.resolve(approval_id, outcome, actor) else "already_resolved"
