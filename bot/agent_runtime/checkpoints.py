"""Real git-backed checkpoints for the agent-loop engine's workspaces —
/rollback, /undo, /branch, /compress, /worktree. Genuine `git` plumbing
scoped to one workspace directory (see tools.py's resolve_workspace), not a
simulated or in-memory history.

Auto-checkpointing (see api_backend.py's _run_one_tool) commits after every
successful run_shell/write_file call, on whatever branch is currently
checked out — the same thing a careful human would do while editing, not a
hidden side-branch. That means a workspace that happens to already be a
real git repo with its own history keeps that history untouched; the agent
just adds ordinary commits on top of it.

Every workspace's very first checkpoint call also records a "session start"
commit hash in .git/agent_checkpoint_base (a plain bookkeeping file, not a
git object) — rollback/undo/compress only ever operate on commits made
*after* that point, so they can never discard history that predates the
agent's own checkpointing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class CheckpointError(Exception):
    pass


def _run(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
        )
    except OSError as exc:
        raise CheckpointError(f"git not available: {exc}") from exc
    if proc.returncode != 0:
        raise CheckpointError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc.stdout.strip()


def ensure_repo(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        _run(["init"], workspace)
    # A fresh `git init` has no committer identity configured on most CI/
    # server boxes — set a local (repo-scoped, not global) identity so
    # checkpoint commits never fail with "please tell me who you are".
    try:
        _run(["config", "user.name"], workspace)
    except CheckpointError:
        _run(["config", "user.name", "BotServer Agent"], workspace)
    try:
        _run(["config", "user.email"], workspace)
    except CheckpointError:
        _run(["config", "user.email", "agent@botserver.local"], workspace)


def _base_marker(workspace: Path) -> Path:
    return workspace / ".git" / "agent_checkpoint_base"


def session_start_commit(workspace: Path) -> str:
    """The commit hash checkpointing for this workspace started from —
    computed once and cached in the marker file, so it stays fixed even as
    HEAD moves forward with each new checkpoint."""
    ensure_repo(workspace)
    marker = _base_marker(workspace)
    if marker.exists():
        return marker.read_text().strip()
    try:
        head = _run(["rev-parse", "HEAD"], workspace)
    except CheckpointError:
        _run(["commit", "--allow-empty", "-m", "checkpoint: session start"], workspace)
        head = _run(["rev-parse", "HEAD"], workspace)
    marker.write_text(head)
    return head


def create_checkpoint(workspace: Path, label: str) -> Optional[str]:
    """Commits whatever's currently changed under a "checkpoint: <label>"
    message. Returns the new commit's short hash, or None if there was
    nothing to commit (e.g. a read-only tool call, or a write that produced
    no net change)."""
    session_start_commit(workspace)
    status = _run(["status", "--porcelain"], workspace)
    if not status.strip():
        return None
    _run(["add", "-A"], workspace)
    _run(["commit", "-m", f"checkpoint: {label}"[:200]], workspace)
    return _run(["rev-parse", "--short", "HEAD"], workspace)


def list_checkpoints(workspace: Path, limit: int = 20) -> list[dict]:
    """Checkpoint commits made since this workspace's session start,
    newest first."""
    base = session_start_commit(workspace)
    try:
        head = _run(["rev-parse", "HEAD"], workspace)
    except CheckpointError:
        return []
    if head == base:
        return []
    log = _run(["log", "--format=%H %s", f"{base}..HEAD"], workspace)
    out = []
    for line in log.splitlines()[:limit]:
        commit_hash, _, message = line.partition(" ")
        out.append({"hash": commit_hash, "short": commit_hash[:8], "message": message})
    return out


def rollback(workspace: Path, steps: int = 1) -> str:
    """Hard-resets to `steps` checkpoints back from HEAD — never past this
    workspace's recorded session-start commit, so pre-existing history is
    always safe."""
    checkpoints = list_checkpoints(workspace)
    if not checkpoints:
        return "No checkpoints yet for this workspace — nothing to roll back."
    steps = max(1, steps)
    if steps > len(checkpoints):
        target = session_start_commit(workspace)
        steps = len(checkpoints)
    else:
        # checkpoints[0] is HEAD's own commit; going back `steps` means
        # landing on the commit *before* the `steps`-th checkpoint, i.e.
        # its parent — which is checkpoints[steps] if it exists, else base.
        target = checkpoints[steps]["hash"] if steps < len(checkpoints) else session_start_commit(workspace)
    _run(["reset", "--hard", target], workspace)
    return f"Rolled back {steps} checkpoint(s) — now at {target[:8]}."


def undo(workspace: Path) -> str:
    return rollback(workspace, steps=1)


def branch(workspace: Path, name: str) -> str:
    ensure_repo(workspace)
    session_start_commit(workspace)
    _run(["checkout", "-b", name], workspace)
    return f"Switched to new branch {name!r} — future checkpoints commit here until you /branch again."


def compress(workspace: Path) -> str:
    """Squashes every checkpoint commit since session start into one,
    keeping the working tree's current content but collapsing the history
    those small auto-commits accumulated."""
    base = session_start_commit(workspace)
    try:
        head = _run(["rev-parse", "HEAD"], workspace)
    except CheckpointError:
        return "No checkpoints yet for this workspace — nothing to compress."
    if head == base:
        return "No checkpoints yet for this workspace — nothing to compress."
    _run(["reset", "--soft", base], workspace)
    status = _run(["status", "--porcelain"], workspace)
    if status.strip():
        _run(["commit", "-m", "checkpoint: compressed"], workspace)
        return "Compressed this session's checkpoint history into one commit."
    # reset --soft to base with no net diff (e.g. edits that cancelled out) —
    # nothing to commit, base already reflects the current tree.
    return "Compressed — no net changes remained, history cleared back to session start."


def worktree(workspace: Path, name: str) -> str:
    """Adds a linked worktree next to `workspace` (not inside it — git
    forbids nesting a worktree under the repo it's linked to) on a new
    branch of the same name, for parallel exploration without disturbing
    the main working directory."""
    ensure_repo(workspace)
    session_start_commit(workspace)
    dest = workspace.parent / f"{workspace.name}-{name}"
    if dest.exists():
        raise CheckpointError(f"{dest} already exists")
    _run(["worktree", "add", str(dest), "-b", name], workspace)
    return f"Created worktree {name!r} at {dest}."
