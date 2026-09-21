"""Git worktrees for sub-agents that must not disturb the parent's files (roadmap P4).

A sub-agent started with `isolation: worktree` works in its own checkout of the repository on
its own branch. The parent's working folder is untouched while it runs. When it finishes:

* if it changed nothing (no uncommitted changes, no new commits) the worktree and its branch
  are removed;
* otherwise they are kept, and the result says where they are and on which branch, so a person
  (or the parent) can review and merge them. Nothing is ever merged automatically.

The worktree lives in the agent state folder, outside the repository. The workspace must be
inside a git repository; otherwise isolation is refused with a clear message.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from bot.agent_runtime.errors import ToolError
from bot.agent_runtime.state import state_dir

GIT_TIMEOUT_S = 60


@dataclass
class Worktree:
    path: Path
    branch: str
    repo: Path
    base: str            # commit it was created from


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"git failed: {exc}")
    if check and proc.returncode != 0:
        raise ToolError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def repo_root(workspace: Path) -> Path:
    out = _git(["rev-parse", "--show-toplevel"], Path(workspace), check=False)
    if not out:
        raise ToolError("isolation: worktree needs the working folder to be inside a git repository")
    return Path(out)


def create(workspace: Path, label: str) -> Worktree:
    workspace = Path(workspace).resolve()
    root = repo_root(workspace)
    base = _git(["rev-parse", "--verify", "--quiet", "HEAD"], root, check=False)
    if not base:
        raise ToolError("isolation: worktree needs the repository to have at least one commit")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label)[:30].strip("-") or "agent"
    branch = f"abp/{safe}-{uuid.uuid4().hex[:6]}"
    folder = state_dir("worktrees") / f"{hashlib.sha1(str(root).encode()).hexdigest()[:8]}-{branch.split('/', 1)[1]}"
    _git(["worktree", "add", "-b", branch, str(folder), base], root)
    # The child starts in the same sub-folder of the repository the parent was in.
    try:
        rel = workspace.relative_to(root)
    except ValueError:
        rel = Path(".")
    return Worktree(path=(folder / rel).resolve(), branch=branch, repo=root, base=base)


def _folder(wt: Worktree) -> Path:
    return wt.path if (wt.path / ".git").exists() else next((p for p in [wt.path, *wt.path.parents] if (p / ".git").exists()), wt.path)


def finish(wt: Worktree) -> dict:
    """Remove the worktree if nothing changed; otherwise keep it. Returns a description for the result."""
    folder = _folder(wt)
    dirty = _git(["status", "--porcelain"], folder, check=False)
    head = _git(["rev-parse", "--verify", "--quiet", "HEAD"], folder, check=False)
    committed = head and head != wt.base
    if not dirty and not committed:
        _git(["worktree", "remove", "--force", str(folder)], wt.repo, check=False)
        _git(["branch", "-D", wt.branch], wt.repo, check=False)
        return {"kept": False}
    changed = _git(["diff", "--stat", "HEAD"], folder, check=False).splitlines()[-1:] if dirty else []
    return {"kept": True, "path": str(folder), "branch": wt.branch, "uncommitted": bool(dirty),
            "commits": bool(committed), "summary": changed[0].strip() if changed else ""}
