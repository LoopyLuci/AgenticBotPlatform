"""Handoff packs: everything a different model needs to resume a task without the old conversation.

A pack is one JSON file per task in data/agent/capacity/handoff/. It holds the goal, the plan, the files touched, the diff so
far, the test status, notes and the last error. `write()` replaces the pack, `update()` merges fields into it, `read()` returns
it, `resume_prompt()` renders it as the opening message for the model that takes over, and `attach_to_board()` puts a copy on
the work board task (as the task's result while it runs, plus a note) so the pack travels with the task.

    pack = handoff.write(HandoffPack(task_id="t1", goal="fix the failing auth test", plan=["read auth.py", "patch"]))
    handoff.update("t1", files_touched=["auth.py"], last_error="AssertionError ...")
    text = handoff.resume_prompt(handoff.read("t1"))
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

VERSION = 1
MAX_DIFF = 24_000
MAX_TEXT = 4_000
MAX_LIST = 60
STATUSES = ("running", "finished", "failed", "interrupted")
TEST_STATES = ("unknown", "not_run", "passing", "failing")


@dataclass
class HandoffPack:
    task_id: str
    goal: str = ""
    plan: list = field(default_factory=list)            # steps, in order; a step may be a string or {"step": ..., "done": bool}
    files_touched: list = field(default_factory=list)
    diff: str = ""                                      # the work so far as a unified diff (capped)
    test_status: dict = field(default_factory=lambda: {"status": "unknown", "command": "", "summary": ""})
    notes: list = field(default_factory=list)           # what the previous model learned: decisions, dead ends, gotchas
    last_error: str = ""
    board: str = ""
    model: str = ""                                     # the model that wrote it (informational)
    workspace: str = ""
    status: str = "running"                             # running | finished | failed | interrupted (autopack.py keeps it)
    capacity: str = ""                                  # the capacity state when it was last written
    version: int = VERSION
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def pack_dir() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("capacity", "handoff")


def _path(task_id: str) -> Path:
    from bot.agent_runtime.state import safe_name

    return pack_dir() / f"{safe_name(str(task_id), 120)}.json"


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 40] + f"\n...[{len(text) - limit + 40} characters cut]"


def _clean(pack: HandoffPack) -> HandoffPack:
    if not str(pack.task_id or "").strip():
        raise ValueError("a handoff pack needs a task_id")
    pack.goal = _clip(pack.goal, MAX_TEXT)
    pack.plan = [(_clip(s, 500) if not isinstance(s, dict) else {**s, "step": _clip(s.get("step", ""), 500)}) for s in list(pack.plan)[:MAX_LIST]]
    pack.files_touched = [str(f)[:300] for f in list(dict.fromkeys(pack.files_touched))[:200]]
    pack.diff = _clip(pack.diff, MAX_DIFF)
    ts = pack.test_status if isinstance(pack.test_status, dict) else {"status": str(pack.test_status)}
    status = str(ts.get("status") or "unknown")
    pack.test_status = {"status": status if status in TEST_STATES else "unknown", "command": _clip(ts.get("command", ""), 300),
                        "summary": _clip(ts.get("summary", ""), 1500)}
    pack.notes = [_clip(n, 1000) for n in list(pack.notes)[-MAX_LIST:]]
    pack.last_error = _clip(pack.last_error, 2000)
    pack.status = pack.status if pack.status in STATUSES else "running"
    pack.capacity = str(pack.capacity or "")[:20]
    pack.model = str(pack.model or "")[:200]
    pack.version = VERSION
    return pack


def write(pack: HandoffPack, *, now: Optional[float] = None) -> HandoffPack:
    """Save (replace) the pack for its task. Atomic: a reader never sees half a file."""
    now = time.time() if now is None else now
    pack = _clean(pack)
    pack.created_at = pack.created_at or now
    pack.updated_at = now
    target = _path(pack.task_id)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pack-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(pack.to_dict(), fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return pack


def from_dict(data: dict) -> HandoffPack:
    known = {f.name for f in fields(HandoffPack)}
    return HandoffPack(**{k: v for k, v in (data or {}).items() if k in known})


def read(task_id: str) -> Optional[HandoffPack]:
    try:
        return from_dict(json.loads(_path(task_id).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def update(task_id: str, *, append_notes: Optional[list] = None, now: Optional[float] = None, **changes: Any) -> HandoffPack:
    """Merge fields into the pack (created when missing). `append_notes` adds to the notes instead of replacing them."""
    pack = read(task_id) or HandoffPack(task_id=task_id)
    known = {f.name for f in fields(HandoffPack)} - {"task_id", "version", "created_at", "updated_at"}
    for k, v in changes.items():
        if k not in known:
            raise ValueError(f"unknown handoff field: {k}")
        setattr(pack, k, v)
    if append_notes:
        pack.notes = list(pack.notes) + [str(n) for n in append_notes]
    return write(pack, now=now)


def list_packs(limit: int = 100) -> list[dict]:
    out = []
    for p in sorted(pack_dir().glob("*.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"task_id": d.get("task_id"), "goal": str(d.get("goal", ""))[:120], "model": d.get("model", ""),
                    "test_status": (d.get("test_status") or {}).get("status", "unknown"), "updated_at": d.get("updated_at"),
                    "status": d.get("status", "running"), "files": len(d.get("files_touched") or [])})
    return out


def delete(task_id: str) -> bool:
    """Remove the pack ABP wrote for this task (a finished task no longer needs it)."""
    try:
        _path(task_id).unlink()
        return True
    except OSError:
        return False


def resume_prompt(pack: HandoffPack) -> str:
    """The opening message for the model taking over. Plain text, no secrets beyond what the pack holds."""
    lines = [f"You are taking over task {pack.task_id}" + (f" from {pack.model}" if pack.model else "") +
             ". You do not have the earlier conversation; this is everything that was recorded.", "", f"GOAL: {pack.goal or '(not recorded)'}"]
    if pack.plan:
        lines.append("PLAN:")
        for i, s in enumerate(pack.plan, 1):
            if isinstance(s, dict):
                lines.append(f"  {i}. [{'x' if s.get('done') else ' '}] {s.get('step', '')}")
            else:
                lines.append(f"  {i}. {s}")
    if pack.files_touched:
        lines.append("FILES TOUCHED: " + ", ".join(pack.files_touched))
    ts = pack.test_status
    lines.append(f"TESTS: {ts.get('status', 'unknown')}" + (f" (run: {ts['command']})" if ts.get("command") else "") +
                 (f" - {ts['summary']}" if ts.get("summary") else ""))
    if pack.last_error:
        lines += ["LAST ERROR:", pack.last_error]
    if pack.notes:
        lines.append("NOTES:")
        lines += [f"  - {n}" for n in pack.notes]
    if pack.diff:
        lines += ["DIFF SO FAR (already applied in the workspace; do not redo it):", pack.diff]
    lines += ["", "Check the workspace matches this, then continue from the first unfinished plan step. "
              "Update the notes as you learn things."]
    return "\n".join(lines)


def attach_to_board(board, pack: HandoffPack, agent: str, fence: int) -> dict:
    """Put a copy of the pack on the work board task (its `result` while it is running, and a note). The board client has no
    separate attachment call, so this uses set_state(running) - the task stays running and owned by `agent`. Returns the task."""
    body = pack.to_dict()
    body["diff"] = _clip(body["diff"], 12_000)      # the board caps a result at 64 KB; the full pack stays in the file
    note = _clip(f"handoff pack saved {time.strftime('%H:%M:%S', time.gmtime(pack.updated_at or time.time()))}Z: "
                 f"{pack.test_status.get('status', 'unknown')} tests, {len(pack.files_touched)} file(s)", 300)
    return board.set_state(pack.task_id, agent, fence, "running", result={"handoff_pack": body}, note=note)


def from_board_task(task: dict) -> Optional[HandoffPack]:
    """The pack stored on a board task's result, if any."""
    res = (task or {}).get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            return None
    if isinstance(res, dict) and isinstance(res.get("handoff_pack"), dict):
        return from_dict(res["handoff_pack"])
    return None

