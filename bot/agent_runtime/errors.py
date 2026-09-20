"""Shared by tools.py and the modules that register tools of their own, so neither
has to import the other to raise or to check a path."""

from __future__ import annotations

from pathlib import Path


class ToolError(Exception):
    """A tool call that failed in a way the model should be told about (bad
    input, missing file, refused path). The loop returns it as text; it is not a crash."""


def safe_path(workspace: Path, rel_path: str) -> Path:
    """Resolve `rel_path` inside `workspace`, refusing anything that lands outside it
    (including via `..` or a symlink)."""
    candidate = (workspace / rel_path).resolve() if not Path(rel_path).is_absolute() else Path(rel_path).resolve()
    try:
        candidate.relative_to(Path(workspace).resolve())
    except ValueError:
        raise ToolError(f"path {rel_path!r} is outside the working directory ({workspace})")
    return candidate
