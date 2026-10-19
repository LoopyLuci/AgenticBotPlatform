"""A plan's proof command: validated, then run only through the sandbox.

A plan comes from a model, so its proof is never run as a shell command. Only two shapes are allowed:

    python -m pytest [-q] [-x] [-k EXPR] <test paths or node ids>      (the run_tests tool's own validation and runner)
    ruff check <paths>                                                  (paths: letters, digits, . _ - / only)

Test targets go through `bot.agent_runtime.run_tests.clean_targets` (no shell syntax, no options, no `..`, nothing outside the
workspace) and run through `shell._run_fresh`, the runner behind run_tests, which starts the process with `sandbox.start` - the
configured Windows job / Docker / ssh sandbox and its network policy, with a timeout that stops the process tree. Anything else is
refused with a reason and never executed.

`CHANGED_TESTS` is a third, symbolic proof (the deterministic fallback plan uses it): the test files the task's branch added or
changed since its start, run as `python -m pytest <those files>`. A task that touched no test file has nothing to prove with.

`run(..., env=...)` runs the proof inside the worker's own agent environment (the same interpreter first on PATH and the same
sandbox grants, e.g. the venv readable inside the AppContainer), under `ENV_LOCK`, the lock agent turns hold: that environment is
process-global. Without it, `python` is whatever the PATH finds first, which may not have the project's packages.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Callable, ContextManager, Optional

from bot.agent_runtime import run_tests
from bot.agent_runtime.errors import ToolError
from bot.conductor import cache as cachelib

CHANGED_TESTS = "changed-tests"
ENV_LOCK = threading.Lock()       # held by every agent turn and every proof run in an agent environment: both change process-global state

_SAFE_FLAGS = {"-q", "-x", "-v", "-rf"}
_PREFIX = re.compile(r"^\s*(?:python3?(?:\.exe)?\s+-m\s+)?pytest(?=\s|$)", re.I)
_RUFF = re.compile(r"^\s*(?:python3?(?:\.exe)?\s+-m\s+)?ruff\s+check(?=\s|$)", re.I)
_PATH = re.compile(r"^[\w./-]+$")


class ProofRefused(ValueError):
    pass


def parse(command: str, workspace: Optional[Path] = None) -> tuple[str, list[str], str]:
    """(kind "pytest"|"ruff", validated targets, -k expression). Raises ProofRefused with the reason. With no `workspace` only the
    shape is checked (planning time); with one, the targets must exist inside it."""
    text = str(command or "").strip()
    if not text:
        raise ProofRefused("the proof is empty")
    if re.search(r"[;&|<>`$%^\n\r]", text):
        raise ProofRefused("shell syntax (; & | < > ` $ %) is not allowed in a proof")
    try:
        parts = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ProofRefused(f"the proof cannot be read: {exc}") from exc
    ws = Path(workspace).resolve() if workspace else Path.cwd()
    if _RUFF.match(text):
        paths = parts[parts.index("check") + 1:] if "check" in parts else []
        if not paths or len(paths) > 20 or not all(_PATH.match(p) and ".." not in p.split("/") and not p.startswith(("/", "-")) for p in paths):
            raise ProofRefused("ruff check takes repository-relative paths only (letters, digits, . _ - /; no options, no '..')")
        if workspace and not all((ws / p).exists() for p in paths):
            raise ProofRefused("a ruff path does not exist in the workspace")
        return "ruff", paths, ""
    m = _PREFIX.match(text)
    if not m:
        raise ProofRefused("only `python -m pytest <test paths>` and `ruff check <paths>` are allowed as a proof")
    rest = shlex.split(text[m.end():], posix=True)
    targets: list[str] = []
    k = ""
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in _SAFE_FLAGS:
            pass
        elif arg == "-k" and i + 1 < len(rest):
            i += 1
            k = rest[i]
        else:
            targets.append(arg)
        i += 1
    try:
        clean = run_tests.clean_targets(ws, targets, must_exist=bool(workspace))
        k = run_tests.clean_k(k) if k else ""
    except ToolError as exc:
        raise ProofRefused(str(exc).split(" Call run_tests")[0][:300]) from exc
    return "pytest", clean, k


def check(command: str, workspace: Optional[Path] = None) -> Optional[str]:
    """None when the proof is allowed, else the reason it is not."""
    if str(command or "").strip() == CHANGED_TESTS:
        return None
    try:
        parse(command, workspace)
        return None
    except ProofRefused as exc:
        return str(exc)


def describe(command: str) -> str:
    """The proof as a worker's prompt should say it."""
    command = str(command or "").strip()
    return "the tests you add or change for this task pass (python -m pytest <those test files>)" if command == CHANGED_TESTS else command


def changed_tests(workspace: Path, base: str) -> list[str]:
    """Test files (tests/**/test_*.py, *_test.py) added, modified or renamed on the branch since `base`, that still exist."""
    if not base:
        return []
    out = subprocess.run(["git", "-C", str(workspace), "diff", "--name-only", "--diff-filter=AMR", f"{base}..HEAD", "--", "tests"],
                         capture_output=True, text=True, timeout=60)
    names = [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []
    return [n for n in names if n.endswith(".py") and (Path(n).name.startswith("test_") or n.endswith("_test.py"))
            and (Path(workspace) / n).is_file()]


def resolve(command: str, workspace: Path, base: str = "") -> str:
    """The concrete proof command: CHANGED_TESTS becomes `python -m pytest <the changed test files>`; anything else is returned as is."""
    if str(command or "").strip() != CHANGED_TESTS:
        return command
    files = changed_tests(workspace, base)
    if not files:
        raise ProofRefused("the task added or changed no test file, so there is nothing to prove it with: add a test for the change")
    return "python -m pytest " + " ".join(files)


async def run(command: str, workspace: Path, timeout_s: Optional[int] = None, cache: Optional[cachelib.Cache] = None, *,
              base: str = "", env: Optional[Callable[[], ContextManager]] = None) -> tuple[bool, str]:
    """Run an allowed proof in the sandbox: (passed, output). Raises ProofRefused for one that is not allowed (nothing is started).
    A proof whose exact tree and command already passed (CacheIt, conductor.cache.enabled) is answered without starting anything.
    `base`: the branch's start (for CHANGED_TESTS). `env`: the worker's agent environment to run in (see the module doc)."""
    ws = Path(workspace).resolve()
    command = await asyncio.to_thread(resolve, command, ws, base)
    kind, targets, k = parse(command, ws)
    cache = cache if cache is not None else cachelib.from_settings()
    tree = await asyncio.to_thread(cachelib.tree_hash, ws) if cache.enabled else ""
    if tree:
        hit = await asyncio.to_thread(cache.lookup, tree, command, targets)
        if hit:
            return True, str(hit.get("output") or "PASSED (cached)")
    if env is None:
        passed, output = await _execute(kind, targets, k, ws, timeout_s)
    else:                                   # in a thread: waiting for the lock must never block the event loop (leases, heartbeats)
        passed, output = await asyncio.to_thread(_execute_in, env, kind, targets, k, ws, timeout_s)
    if passed and tree:
        await asyncio.to_thread(cache.store, tree, command, targets, {"output": "PASSED (cached) " + output[-600:]})
    return passed, output


def _execute_in(env: Callable[[], ContextManager], kind: str, targets: list[str], k: str, ws: Path,
                timeout_s: Optional[int]) -> tuple[bool, str]:
    with ENV_LOCK, env():
        return asyncio.run(_execute(kind, targets, k, ws, timeout_s))


async def _execute(kind: str, targets: list[str], k: str, ws: Path, timeout_s: Optional[int]) -> tuple[bool, str]:
    from bot.agent_runtime import shell

    limit = int(timeout_s or run_tests.timeout_s())
    if kind == "ruff":
        out = await shell._run_fresh("python -m ruff check --no-cache " + " ".join(targets), ws, ws, limit)
        return "[exit code 0]" in out, out[-1500:]
    out = await shell._run_fresh(run_tests.build_command(targets, k), ws, ws, limit)
    summary = run_tests.summarise(out)
    return summary.startswith("PASSED"), summary[-1500:]
