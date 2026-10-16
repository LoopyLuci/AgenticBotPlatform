"""Automatic handoff packs: a native agent or swarm task writes its own pack as it works, so a different model can take over.

    rec = autopack.begin(prompt, model="modelmistress/qwen3", workspace=ws, context=ctx)   # None when there is no task identity
    ... after each tool round:   await rec.after_round(results)       # [(ToolCall, output)]; throttled, written off the loop
    ... at the end:              await rec.finish(ok=True, text=reply)  /  rec.finish(ok=False, error=...)

A turn has a task identity when its caller names one (`context["handoff_task_id"]`) or it runs inside a work-board task
(`bot.coordination.current_work`: every swarm step and board-fed worker). Plain chat turns have none and cost nothing here.

What the pack gets: the goal (the board task's title/detail, else the prompt), files touched (write/edit/patch tools, plus
`git status` when the workspace is a git *worktree* of its own), the diff so far (from git, same condition; above
`capacity.handoff.max_diff_chars` only the `--stat` summary is kept), the test status (run_tests, or a test command run through
run_shell), the last error, and notes. Writes are throttled (`capacity.handoff.min_interval_s`) except right after a test run or
an error, and always at the end. When a pack already exists for the task and was written by a different model, `begin()`
returns the resume prompt from it (`rec.resume_text`) for the caller to put in front of the prompt - the model that takes over,
after a reroute or a restart, starts from the pack instead of the old conversation. A finished pack is not resumed.
The pack is also copied onto the board task (its `result`) at most once a minute; the step's own result replaces it on success.
Nothing here ever raises into the agent loop.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from bot.capacity import governor, handoff

logger = logging.getLogger("bot.capacity.autopack")

WRITE_TOOLS = ("write_file", "edit_file", "multi_edit")
TEST_RE = re.compile(r"\b(pytest|py\.test|cargo test|npm (?:run )?test|ruff check|go test|unittest)\b", re.I)
BOARD_EVERY_S = 60.0
MAX_NOTES = 20
# The recorder of the turn running in this context. A turn started inside it (a delegate, a sub-agent) records nothing of its own
# unless its caller names a task id, so a child never overwrites its parent's pack.
active: contextvars.ContextVar[Optional["Recorder"]] = contextvars.ContextVar("capacity_pack", default=None)


def _cfg() -> dict:
    return governor.settings().get("handoff") or {}


def task_identity(context: Optional[dict]) -> tuple[str, Any]:
    """(task id, the WorkSession or None) for this turn, or ("", None)."""
    ctx = context or {}
    work = None
    try:
        from bot import coordination

        work = coordination.current_work.get()
    except Exception:  # noqa: BLE001
        work = None
    explicit = str(ctx.get("handoff_task_id") or "").strip()
    if explicit:
        return explicit, work
    return (work.task_id, work) if work is not None else ("", None)


def _is_worktree(ws: Path) -> bool:
    """A git linked worktree (its .git is a file): the changes in it are this task's alone."""
    try:
        return (ws / ".git").is_file()
    except OSError:
        return False


def _git(ws: Path, *args: str, timeout: float = 8.0) -> str:
    try:
        done = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_snapshot(ws: Path, max_diff: int) -> dict:
    """{"files": [...], "diff": str} for a worktree: the full diff when it fits `max_diff`, else the --stat summary."""
    status = _git(ws, "status", "--porcelain", "--untracked-files=normal")
    files = [ln[3:].strip().strip('"') for ln in status.splitlines() if len(ln) > 3][:200]
    diff = _git(ws, "diff", "HEAD", "--no-color", "-U2")
    if len(diff) > max_diff:
        stat = _git(ws, "diff", "HEAD", "--no-color", "--stat").strip()
        diff = (f"(the full diff is {len(diff)} characters; here is its summary - run `git diff HEAD` in the workspace for the rest)\n"
                + stat)[:max_diff]
    untracked = [ln[3:].strip() for ln in status.splitlines() if ln.startswith("??")]
    if untracked and diff:
        diff += "\n(new files not in the diff: " + ", ".join(untracked[:20]) + ")"
    return {"files": files, "diff": diff}


def parse_tests(command: str, output: str) -> Optional[dict]:
    """The test status a tool output shows, or None when it was not a test run."""
    low = output.lower()
    passed = re.search(r"(\d+) passed", low)
    failed = re.search(r"(\d+) (?:failed|errors?)\b", low)
    if not (passed or failed or "no tests ran" in low or "all checks passed" in low):
        return None
    status = "failing" if failed else "passing"
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if re.search(r"\d+ (?:passed|failed)|all checks passed", ln, re.I)),
                   lines[-1] if lines else "")
    return {"status": status, "command": command[:300], "summary": summary[:600]}


class Recorder:
    def __init__(self, task_id: str, work: Any, pack: handoff.HandoffPack, workspace: Optional[Path], resume_text: str = ""):
        self.task_id, self.work, self.pack, self.workspace = task_id, work, pack, workspace
        self.resume_text = resume_text
        self._last_write = time.monotonic()           # begin() has just written the first pack
        self._last_board = 0.0
        self._dirty = False
        self._urgent = False
        self.writes = 0

    # ---- recording -----------------------------------------------------------------------------------
    def note(self, text: str) -> None:
        self.pack.notes = (list(self.pack.notes) + [str(text)[:500]])[-MAX_NOTES:]
        self._dirty = True

    def _observe(self, tc: Any, out: str) -> None:
        name, args = getattr(tc, "name", ""), getattr(tc, "arguments", None) or {}
        out = str(out or "")
        refused = out.startswith(("Error", "Denied"))
        if name in WRITE_TOOLS and args.get("path") and not refused:
            if str(args["path"]) not in self.pack.files_touched:
                self.pack.files_touched = list(self.pack.files_touched) + [str(args["path"])]
                self._dirty = True
        elif name == "apply_patch" and not refused:
            for path in re.findall(r"^\+\+\+ b/(\S+)", str(args.get("patch") or ""), re.M):
                if path not in self.pack.files_touched:
                    self.pack.files_touched = list(self.pack.files_touched) + [path]
                    self._dirty = True
        command = ""
        if name == "run_tests":
            command = "run_tests " + " ".join(map(str, args.get("paths") or []))
        elif name == "run_shell" and TEST_RE.search(str(args.get("command") or "")):
            command = str(args.get("command") or "")
        if command:
            got = parse_tests(command, out)
            if got:
                self.pack.test_status = got
                self._dirty = self._urgent = True
        if refused or "Traceback (most recent call last)" in out[:2000]:
            self.pack.last_error = f"{name}: {out[:1500]}"
            self._dirty = self._urgent = True

    async def after_round(self, results: list) -> None:
        """One round of tool calls finished (`[(tool call, output)]`). Throttled; never raises."""
        try:
            for tc, out in results:
                self._observe(tc, out)
            if not self._dirty:
                return
            gap = float(_cfg().get("min_interval_s", 20))
            if self._urgent or time.monotonic() - self._last_write >= gap:
                await self.flush()
        except Exception:  # noqa: BLE001
            logger.debug("handoff: could not record a tool round", exc_info=True)

    # ---- writing -------------------------------------------------------------------------------------
    def _snapshot(self) -> None:
        cfg = _cfg()
        if cfg.get("git_diff", True) and self.workspace is not None and _is_worktree(self.workspace):
            snap = git_snapshot(self.workspace, int(cfg.get("max_diff_chars", 12000)))
            self.pack.diff = snap["diff"]
            self.pack.files_touched = list(dict.fromkeys(list(self.pack.files_touched) + snap["files"]))
        try:
            self.pack.capacity = governor.get_state()["state"]
        except Exception:  # noqa: BLE001
            pass

    def _write(self) -> None:
        self._snapshot()
        handoff.write(self.pack)
        self.writes += 1
        self._last_write = time.monotonic()
        self._dirty = self._urgent = False

    async def flush(self) -> None:
        await asyncio.to_thread(self._write)
        await self._to_board()

    async def _to_board(self, *, force: bool = False) -> None:
        work = self.work
        if work is None or work.lost or (not force and time.monotonic() - self._last_board < BOARD_EVERY_S):
            return
        self._last_board = time.monotonic()
        try:
            from bot.coordination.session import acall

            await acall(work.board, handoff.attach_to_board, work.board, self.pack, work.agent, work.fence)
        except Exception:  # noqa: BLE001 - the file is the pack; the board copy is a courtesy
            logger.debug("handoff: could not copy the pack to the board", exc_info=True)

    async def finish(self, ok: bool, text: str = "", error: str = "", *, interrupted: bool = False) -> None:
        try:
            self.pack.status = "finished" if ok else ("interrupted" if interrupted else "failed")
            if ok and text:
                self.note("finished: " + str(text).strip().replace("\n", " ")[:400])
            if error:
                self.pack.last_error = str(error)[:1500]
            await asyncio.shield(asyncio.to_thread(self._write))
            if not ok:
                await self._to_board(force=True)
        except BaseException:  # noqa: BLE001 - writing the last word must not change how the turn ends
            logger.debug("handoff: could not write the final pack", exc_info=True)


def begin(prompt: str, *, model: str, workspace: Any = None, context: Optional[dict] = None) -> Optional[Recorder]:
    """Start recording this turn, or None (no task identity / automatic packs off / anything went wrong)."""
    try:
        cfg = _cfg()
        if not cfg.get("auto", True) or not governor.settings().get("enabled", True):
            return None
        task_id, work = task_identity(context)
        if not task_id or (active.get() is not None and not str((context or {}).get("handoff_task_id") or "").strip()):
            return None
        ws = Path(str(workspace)) if workspace else None
        old = handoff.read(task_id)
        resume = ""
        if old is not None and old.status != "finished" and old.model and old.model != model:
            resume = handoff.resume_prompt(old)
            old.notes = (list(old.notes) + [f"resumed on {model} (it was {old.model}; capacity {governor.get_state()['state']})"])[-MAX_NOTES:]
        pack = old or handoff.HandoffPack(task_id=task_id)
        task = getattr(work, "task", None) or {}
        if not pack.goal:
            pack.goal = (f"{task.get('title', '')}\n{task.get('detail', '')}".strip() if task else "") or str(prompt)[:1500]
        pack.model, pack.status = model, "running"
        pack.workspace = str(ws) if ws else pack.workspace
        pack.board = str(task.get("board") or pack.board or "")
        rec = Recorder(task_id, work, pack, ws, resume_text=resume)
        if old is None or resume:
            handoff.write(pack)
        return rec
    except Exception:  # noqa: BLE001
        logger.debug("handoff: could not start recording", exc_info=True)
        return None
