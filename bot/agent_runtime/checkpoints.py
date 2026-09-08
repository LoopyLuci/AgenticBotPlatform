"""Real git-backed checkpoints for the agent-loop engine's workspaces —
/rollback, /undo, /branch, /compress, /worktree.

Uses a SEPARATE shadow git store (data/checkpoint_store/) rather than the
workspace's own `.git`, mirroring Hermes Agent's own checkpoint-manager
design (confirmed against its real installed source:
tools/checkpoint_manager.py's `store/`/`indexes/` layout, per-project
GIT_DIR/GIT_INDEX_FILE keyed by a path hash, and isolation from the
user's own git config). Real bug this replaces: the previous design ran
`git` directly inside `workspace/.git`, meaning a workspace that already
had its own git history got the agent's checkpoint commits mixed
straight into it, and `rollback`'s `git reset --hard` — even bounded to
commits made after session start — operated on the user's real branch.
Here, checkpoint commits/refs/objects live entirely in the shadow store;
GIT_WORK_TREE points at the real workspace only so git can see/checkpoint
its actual file contents. A workspace's own `.git` (if it has one) is
never opened, read, or written by any function in this module.

Auto-checkpointing (see api_backend.py's _run_one_tool) commits after
every successful run_shell/write_file call. Every workspace's first
checkpoint call also records a "session start" commit hash (now in the
shadow store, not a file under the workspace) — rollback/undo/compress
only ever operate on commits made *after* that point.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Optional


class CheckpointError(Exception):
    pass


def _store_root() -> Path:
    from bot.envfile import PROJECT_ROOT

    root = PROJECT_ROOT / "data" / "checkpoint_store"
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "indexes").mkdir(parents=True, exist_ok=True)
    (root / "bases").mkdir(parents=True, exist_ok=True)
    return root


def _workspace_key(workspace: Path) -> str:
    """Stable across process restarts — the same workspace path always
    maps to the same shadow git-dir/index, matching Hermes's own
    hash-keyed per-project store layout (object-level dedup potential
    across workspaces sharing the same store, same rationale as Hermes's
    design)."""
    resolved = str(Path(workspace).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _git_dir(workspace: Path) -> Path:
    return _store_root() / "objects" / _workspace_key(workspace)


def _index_file(workspace: Path) -> Path:
    return _store_root() / "indexes" / _workspace_key(workspace)


def _base_marker(workspace: Path) -> Path:
    return _store_root() / "bases" / _workspace_key(workspace)


def _excludes_file() -> Path:
    """A single shared excludes file telling our shadow git to never
    track the workspace's own `.git` directory (if it has one) as
    ordinary file content — without this, `git add -A` under
    --work-tree would happily add the user's ENTIRE real `.git`
    internals as tracked blobs in our shadow store, since our git-dir
    lives elsewhere and `.git` looks like just another directory to it."""
    path = _store_root() / "checkpoint.gitignore"
    if not path.exists():
        path.write_text(".git/\n.git\n", encoding="utf-8")
    return path


def _env(workspace: Path) -> dict:
    env = dict(os.environ)
    env["GIT_DIR"] = str(_git_dir(workspace))
    env["GIT_WORK_TREE"] = str(Path(workspace).resolve())
    env["GIT_INDEX_FILE"] = str(_index_file(workspace))
    # Isolate from the user's real git identity/signing config — this
    # store's commits are internal bookkeeping, never meant to be signed,
    # pushed, or attributed via the user's own global/system gitconfig.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _run(args: list[str], workspace: Path) -> str:
    workspace = Path(workspace)
    try:
        proc = subprocess.run(
            ["git", "-c", f"core.excludesFile={_excludes_file()}", "-c", "commit.gpgsign=false", *args],
            cwd=str(workspace.resolve()) if workspace.exists() else str(workspace),
            capture_output=True, text=True, timeout=30, env=_env(workspace),
        )
    except OSError as exc:
        raise CheckpointError(f"git not available: {exc}") from exc
    if proc.returncode != 0:
        raise CheckpointError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc.stdout.strip()


def ensure_repo(workspace: Path) -> None:
    if not _git_dir(workspace).exists():
        _run(["init"], workspace)
    # A fresh shadow repo has no committer identity — set one local to
    # THIS shadow git-dir (never the user's global config, already
    # isolated above) so checkpoint commits never fail with "please tell
    # me who you are".
    try:
        _run(["config", "user.name"], workspace)
    except CheckpointError:
        _run(["config", "user.name", "BotServer Agent"], workspace)
    try:
        _run(["config", "user.email"], workspace)
    except CheckpointError:
        _run(["config", "user.email", "agent@botserver.local"], workspace)


def session_start_commit(workspace: Path) -> str:
    """The commit hash checkpointing for this workspace started from —
    computed once and cached in the shadow store's own marker file, so
    it stays fixed even as HEAD moves forward with each new checkpoint."""
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


def safe_restore_plan(workspace: Path, target: str) -> str:
    """A preview of what rolling back to `target` would change in the
    real working tree — a real `git diff --stat` against the shadow
    store's current HEAD, surfaced to a human before /rollback actually
    executes when confirm_destructive is on (same confirm-flow
    convention cmd_desktop's stop/restart already uses). Empty string if
    there would be no changes."""
    try:
        head = _run(["rev-parse", "HEAD"], workspace)
    except CheckpointError:
        return ""
    if head == target:
        return ""
    return _run(["diff", "--stat", target, head], workspace)


def rollback(workspace: Path, steps: int = 1) -> str:
    """Hard-resets the real working tree to `steps` checkpoints back from
    HEAD — never past this workspace's recorded session-start commit.
    Operates only on the shadow store's own history; the workspace's own
    `.git` (if any) is never touched."""
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
    the main working directory. The worktree still uses the shadow
    store's own git-dir, not the workspace's real .git."""
    ensure_repo(workspace)
    session_start_commit(workspace)
    dest = Path(workspace).parent / f"{Path(workspace).name}-{name}"
    if dest.exists():
        raise CheckpointError(f"{dest} already exists")
    _run(["worktree", "add", str(dest), "-b", name], workspace)
    return f"Created worktree {name!r} at {dest}."


def gc_old_checkpoints(retention_days: int) -> int:
    """Fully removes shadow-store data (objects, index, base marker) for
    any workspace whose base marker hasn't been touched in longer than
    [retention_days] — matches Hermes's own retention behavior. A
    checkpoint history is ephemeral session-scoped bookkeeping, not
    something worth keeping indefinitely once nobody's touched that
    workspace in that long. Returns the number of workspace stores
    removed. Safe to call on a schedule; a store touched more recently
    than the cutoff is left alone regardless of its commit count."""
    import shutil
    import stat

    def _force_remove(func, path, exc_info):
        # git marks its object files read-only on Windows — shutil.rmtree
        # fails on those with PermissionError unless the read-only bit is
        # cleared first. Confirmed live: rmtree silently (or loudly,
        # without this handler) failed to remove a real git-dir this way.
        os.chmod(path, stat.S_IWRITE)
        func(path)

    store = _store_root()
    cutoff = time.time() - retention_days * 86400
    removed = 0
    bases_dir = store / "bases"
    if not bases_dir.is_dir():
        return 0
    for marker in list(bases_dir.iterdir()):
        if not marker.is_file() or marker.stat().st_mtime >= cutoff:
            continue
        key = marker.name
        for path in (store / "objects" / key, store / "indexes" / key):
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path, onerror=_force_remove)
                else:
                    os.chmod(path, stat.S_IWRITE)
                    path.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        removed += 1
    return removed
