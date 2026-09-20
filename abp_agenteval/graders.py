"""Graders: small, deterministic checks over a finished run. Each takes what it
needs and returns a callable that yields a `Check`, so a task lists them in order."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .task import Check, Context

Grader = Callable[[Context], Check]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def file_equals(rel: str, expected: str, *, strip: bool = True) -> Grader:
    def check(ctx: Context) -> Check:
        got = _read(ctx.workspace / rel)
        if got is None:
            return Check(f"{rel} exists", False, "file is missing")
        a, b = (got.strip(), expected.strip()) if strip else (got, expected)
        return Check(f"{rel} equals expected", a == b, "" if a == b else f"got {got[:80]!r}")
    return check


def file_contains(rel: str, needle: str) -> Grader:
    def check(ctx: Context) -> Check:
        got = _read(ctx.workspace / rel)
        ok = got is not None and needle in got
        return Check(f"{rel} contains {needle!r}", ok, "" if ok else ("file is missing" if got is None else "not found"))
    return check


def file_absent(rel: str) -> Grader:
    def check(ctx: Context) -> Check:
        present = (ctx.workspace / rel).exists()
        return Check(f"{rel} was not created", not present, "it exists" if present else "")
    return check


def command_passes(argv: list[str], *, label: str = "") -> Grader:
    """Run a command in the workspace after the agent finishes; `python` means this interpreter."""
    def check(ctx: Context) -> Check:
        cmd = [sys.executable if a == "python" else a for a in argv]
        try:
            proc = subprocess.run(cmd, cwd=str(ctx.workspace), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check(label or " ".join(argv), False, str(exc))
        return Check(label or " ".join(argv), proc.returncode == 0,
                     "" if proc.returncode == 0 else (proc.stdout + proc.stderr).strip()[-200:])
    return check


def reply_matches(pattern: str, *, label: str = "") -> Grader:
    rx = re.compile(pattern, re.I | re.S)
    def check(ctx: Context) -> Check:
        ok = bool(rx.search(ctx.reply or ""))
        return Check(label or f"reply matches /{pattern}/", ok, "" if ok else f"reply was {ctx.reply[:80]!r}")
    return check


def reply_lacks(text: str, *, label: str = "") -> Grader:
    def check(ctx: Context) -> Check:
        ok = text not in (ctx.reply or "")
        return Check(label or f"reply does not contain {text!r}", ok, "" if ok else "it does")
    return check


def used_tool(tool: str, *, at_least: int = 1) -> Grader:
    def check(ctx: Context) -> Check:
        n = ctx.trace.get("tool_counts", {}).get(tool, 0)
        return Check(f"used {tool}", n >= at_least, f"called {n}x")
    return check


def did_not_use_tool(tool: str) -> Grader:
    def check(ctx: Context) -> Check:
        n = ctx.trace.get("tool_counts", {}).get(tool, 0)
        return Check(f"did not use {tool}", n == 0, f"called {n}x" if n else "")
    return check


def tool_status(tool: str, status: str) -> Grader:
    """At least one call of `tool` ended with `status` (for example a boundary check that must fail)."""
    def check(ctx: Context) -> Check:
        seen = [c["status"] for c in ctx.trace.get("tool_calls", []) if c["tool"] == tool]
        return Check(f"{tool} ended {status}", status in seen, f"saw {seen}")
    return check


def finished_ok() -> Grader:
    def check(ctx: Context) -> Check:
        ok = ctx.error is None and ctx.trace.get("status") == "ok"
        return Check("run finished", ok, ctx.error or ctx.trace.get("status", ""))
    return check


def within_iterations(limit: int) -> Grader:
    def check(ctx: Context) -> Check:
        n = ctx.trace.get("iterations", 0)
        return Check(f"finished within {limit} model calls", n <= limit, f"took {n}")
    return check


def glob_exists(pattern: str, *, label: str = "") -> Grader:
    """At least one file in the workspace matches the glob (dot-folders included)."""
    def check(ctx: Context) -> Check:
        found = list(ctx.workspace.glob(pattern))
        return Check(label or f"a file matches {pattern}", bool(found), "" if found else "none found")
    return check
