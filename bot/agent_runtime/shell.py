"""run_shell and its companions (roadmap P1).

* a per-call `timeout` (default 60 s, at most 10 minutes) and a `cwd` inside the
  working directory;
* `background: true` starts a long-running command (a dev server, a build, a test
  watcher) and returns at once with a job id; `shell_output` reads what it has
  printed since you last looked, `shell_list` shows the session's jobs, `shell_kill`
  stops one;
* when a command times out, is cancelled, or a job is killed, the whole process tree
  is stopped, not just the shell that started it.

Not built: an interactive terminal (a PTY) and a shell that keeps its own state
(cd, exported variables) between calls. Each call is a fresh shell; use `cwd` to run
somewhere else. Both need a platform-specific implementation that has to be tested on
each OS (see docs/agents/ROADMAP.md).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError, safe_path

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
MAX_CAPTURE_CHARS = 400_000        # per command; the tool loop trims what goes back to the model
MAX_JOB_BUFFER = 1_000_000
MAX_JOBS_PER_SESSION = 8
FINISHED_JOB_TTL_S = 3600


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.time)
    buf: bytearray = field(default_factory=bytearray)
    dropped: int = 0                  # bytes discarded from the front once the buffer filled
    read_pos: int = 0                 # absolute position the model has read up to
    returncode: Optional[int] = None
    pump: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.returncode is None


_jobs: dict[str, dict[str, Job]] = {}
_counters: dict[str, itertools.count] = {}   # per session, so a session's first job is always job1


def _spawn_kwargs() -> dict:
    return {} if os.name == "nt" else {"start_new_session": True}


def kill_tree(proc) -> None:
    """Stop the process and everything it started."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _clean_timeout(value) -> int:
    try:
        seconds = int(value) if value is not None else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        raise ToolError("timeout must be a number of seconds")
    if seconds < 1:
        raise ToolError("timeout must be at least 1 second")
    return min(seconds, MAX_TIMEOUT_S)


def _resolve_cwd(workspace: Path, cwd: Optional[str]) -> Path:
    if not cwd:
        return workspace
    target = safe_path(workspace, cwd)
    if not target.is_dir():
        raise ToolError(f"cwd {cwd!r} is not a folder inside the working directory")
    return target


async def _start(command: str, cwd: Path) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_shell(
            command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **_spawn_kwargs())
    except OSError as exc:
        raise ToolError(str(exc)) from exc


def _decode(data: bytes) -> str:
    return data.decode(errors="replace")


async def run_command(command: str, *, workspace: Path, cwd: Optional[str] = None, timeout=None,
                      background: bool = False) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command can't be empty")
    where = _resolve_cwd(workspace, cwd)
    if background:
        return await _start_job(command, where)
    limit = _clean_timeout(timeout)
    proc = await _start(command, where)
    chunks: list[bytes] = []
    size = 0

    async def _drain() -> None:
        nonlocal size
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                break
            if size < MAX_CAPTURE_CHARS:
                chunks.append(data)
                size += len(data)
        await proc.wait()

    reader = asyncio.ensure_future(_drain())
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout=limit)
    except asyncio.TimeoutError:
        kill_tree(proc)
        await _finish(reader)
        so_far = _decode(b"".join(chunks))[-3000:]
        raise ToolError(f"timed out after {limit}s and was stopped. Use background=true for long-running commands."
                        + (f" Output so far:\n{so_far}" if so_far.strip() else ""))
    except asyncio.CancelledError:
        kill_tree(proc)
        await _finish(reader)
        raise
    text = _decode(b"".join(chunks))
    if size >= MAX_CAPTURE_CHARS:
        text += f"\n... (output capped at {MAX_CAPTURE_CHARS} bytes)"
    return f"{text}\n[exit code {proc.returncode}]"


async def _finish(task: asyncio.Task) -> None:
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        task.cancel()


# ---- background jobs --------------------------------------------------------------
def _session_jobs() -> dict[str, Job]:
    return _jobs.setdefault(toolspec.current_session(), {})


def _reap(jobs: dict[str, Job]) -> None:
    now = time.time()
    for jid in [j for j, job in jobs.items() if not job.running and now - job.started > FINISHED_JOB_TTL_S]:
        jobs.pop(jid, None)


async def _pump(job: Job) -> None:
    try:
        while True:
            data = await job.proc.stdout.read(65536)
            if not data:
                break
            job.buf.extend(data)
            over = len(job.buf) - MAX_JOB_BUFFER
            if over > 0:
                del job.buf[:over]
                job.dropped += over
        await job.proc.wait()
        job.returncode = job.proc.returncode
    except asyncio.CancelledError:
        kill_tree(job.proc)
        job.returncode = -1
        raise


async def _start_job(command: str, cwd: Path) -> str:
    jobs = _session_jobs()
    _reap(jobs)
    if sum(1 for j in jobs.values() if j.running) >= MAX_JOBS_PER_SESSION:
        raise ToolError(f"{MAX_JOBS_PER_SESSION} background jobs are already running; stop one with shell_kill first")
    proc = await _start(command, cwd)
    job = Job(id=f"job{next(_counters.setdefault(toolspec.current_session(), itertools.count(1)))}", command=command[:200], cwd=str(cwd), proc=proc)
    job.pump = asyncio.ensure_future(_pump(job))
    jobs[job.id] = job
    return (f"Started background job {job.id} (pid {proc.pid}). Read its output with shell_output, "
            f"list jobs with shell_list, stop it with shell_kill.")


def _get(job_id: str) -> Job:
    job = _session_jobs().get(str(job_id))
    if job is None:
        raise ToolError(f"no background job {job_id!r} in this session (shell_list shows them)")
    return job


def _status(job: Job) -> str:
    return "running" if job.running else f"exited with code {job.returncode}"


async def _shell_output(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    job = _get(inp.get("id"))
    await asyncio.sleep(0)                                   # let the reader deliver anything already waiting
    total = job.dropped + len(job.buf)
    lost = job.dropped > job.read_pos
    start = max(job.read_pos, job.dropped)
    fresh = bytes(job.buf[start - job.dropped:])
    tail = max(500, min(int(inp.get("tail_chars") or 4000), 20000))
    text = _decode(fresh)
    skipped = ""
    if len(text) > tail:
        skipped = f"... ({len(text) - tail} earlier characters not shown)\n"
        text = text[-tail:]
    job.read_pos = total
    gap = "(some output was dropped: the job printed more than the buffer holds)\n" if lost else ""
    body = (gap + skipped + text) if text or skipped else "(no new output)"
    return f"[{job.id}: {_status(job)}]\n{body}"


async def _shell_list(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    jobs = _session_jobs()
    _reap(jobs)
    if not jobs:
        return "No background jobs."
    lines = []
    for job in jobs.values():
        age = int(time.time() - job.started)
        lines.append(f"{job.id}  {_status(job)}  {age}s  {job.command}")
    return "\n".join(lines)


async def _shell_kill(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    job = _get(inp.get("id"))
    if not job.running:
        return f"{job.id} had already {_status(job)}."
    kill_tree(job.proc)
    try:
        await asyncio.wait_for(job.proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    job.returncode = job.proc.returncode if job.proc.returncode is not None else -1
    return f"Stopped {job.id}."


async def kill_session_jobs(session: Optional[str] = None) -> int:
    """Stop every background job of a session (or the current one). Returns how many were running."""
    jobs = _jobs.get(session or toolspec.current_session(), {})
    n = 0
    for job in jobs.values():
        if job.running:
            kill_tree(job.proc)
            n += 1
    return n


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": required}}


def register_all() -> None:
    ident = {"id": {"type": "string", "description": "The job id, e.g. job3."}}
    toolspec.register(
        _schema("shell_output",
                "Read what a background job has printed since you last looked, and whether it is still running.",
                {**ident, "tail_chars": {"type": "integer"}}, ["id"]),
        toolspec.ToolSpec("shell_output", "read", read_only=True, concurrency_safe=True, max_output_chars=12_000,
                          origin="registered"), _shell_output)
    toolspec.register(
        _schema("shell_list", "List this session's background jobs.", {}, []),
        toolspec.ToolSpec("shell_list", "read", read_only=True, concurrency_safe=True, origin="registered"),
        _shell_list)
    toolspec.register(
        _schema("shell_kill", "Stop a background job (and everything it started).", ident, ["id"]),
        toolspec.ToolSpec("shell_kill", "execute", needs_approval=False, origin="registered"), _shell_kill)


register_all()
