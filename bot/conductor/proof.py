"""A plan's proof command: validated, then run only through the sandbox.

A plan comes from a model, so its proof is never run as a shell command. Only two shapes are allowed:

    python -m pytest [-q] [-x] [-k EXPR] <test paths or node ids>      (the run_tests tool's own validation and runner)
    ruff check <paths>                                                  (paths: letters, digits, . _ - / only)

Test targets go through `bot.agent_runtime.run_tests.clean_targets` (no shell syntax, no options, no `..`, nothing outside the
workspace) and run through `shell._run_fresh`, the runner behind run_tests, which starts the process with `sandbox.start` - the
configured Windows job / Docker / ssh sandbox and its network policy, with a timeout that stops the process tree. Anything else is
refused with a reason and never executed.
"""
from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Optional

from bot.agent_runtime import run_tests
from bot.agent_runtime.errors import ToolError
from bot.conductor import cache as cachelib

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
    try:
        parse(command, workspace)
        return None
    except ProofRefused as exc:
        return str(exc)


async def run(command: str, workspace: Path, timeout_s: Optional[int] = None, cache: Optional[cachelib.Cache] = None) -> tuple[bool, str]:
    """Run an allowed proof in the sandbox: (passed, output). Raises ProofRefused for one that is not allowed (nothing is started).
    A proof whose exact tree and command already passed (CacheIt, conductor.cache.enabled) is answered without starting anything."""
    ws = Path(workspace).resolve()
    kind, targets, k = parse(command, ws)
    cache = cache if cache is not None else cachelib.from_settings()
    tree = await asyncio.to_thread(cachelib.tree_hash, ws) if cache.enabled else ""
    if tree:
        hit = await asyncio.to_thread(cache.lookup, tree, command, targets)
        if hit:
            return True, str(hit.get("output") or "PASSED (cached)")
    passed, output = await _execute(kind, targets, k, ws, timeout_s)
    if passed and tree:
        await asyncio.to_thread(cache.store, tree, command, targets, {"output": "PASSED (cached) " + output[-600:]})
    return passed, output


async def _execute(kind: str, targets: list[str], k: str, ws: Path, timeout_s: Optional[int]) -> tuple[bool, str]:
    from bot.agent_runtime import shell

    limit = int(timeout_s or run_tests.timeout_s())
    if kind == "ruff":
        out = await shell._run_fresh("python -m ruff check --no-cache " + " ".join(targets), ws, ws, limit)
        return "[exit code 0]" in out, out[-1500:]
    out = await shell._run_fresh(run_tests.build_command(targets, k), ws, ws, limit)
    summary = run_tests.summarise(out)
    return summary.startswith("PASSED"), summary[-1500:]
