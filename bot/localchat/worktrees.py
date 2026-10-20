"""A git worktree per Local Chat session (PLAN-local-chat.md 4b, S1): the session works on its own branch, in its own folder,
and nothing reaches the base branch without the owner's approval.

    create(sid, base=None)          a worktree for the session on <prefix>/<slug>-<id> from `base` (default: the repo's current
                                    branch), in a short folder outside the repository (local_chat.worktree_root)
    status(sid)                     branch, base, ahead / behind the base, dirty files, head
    diff(sid)                       the session's changes against where it forked from the base (committed and not)
    sync(sid)                       merge the base in; conflicts are listed (the merge is left in progress for the agent)
    commit(sid, message)            commit everything in the worktree; draft_message(sid) proposes a message
    discard(sid, confirm=False)     remove it; with unmerged work this needs confirm=True and says what would be lost
    on_archive(sid)                 an unchanged worktree goes away with the session's archive, a changed one is kept
    queue_merge(sid)                put the session's branch in the conductor's push queue (owner approval; never automatic)
    prune(clean_merged=False)       remove orphan worktrees (no session); optionally the merged, clean ones too

Every git command runs in the session's worktree (or, to add/remove one, in its repository) under one lock per repository.
Merging and pushing happen only through bot/conductor/pusher.py's queue. A worktree is never the live ABP checkout.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from bot.localchat import store

GIT_TIMEOUT_S = 120
DIFF_LIMIT = 200_000
IDENT = ("-c", "user.name=ABP Local Chat", "-c", "user.email=local-chat@agenticbotplatform.local", "-c", "commit.gpgsign=false")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class WorktreeError(RuntimeError):
    """A worktree operation did not happen; the message says why."""


class NeedsConfirmation(WorktreeError):
    """The operation would lose work: `would_lose` says what. Repeat with confirm=True to go ahead."""

    def __init__(self, message: str, would_lose: dict):
        super().__init__(message)
        self.would_lose = would_lose


def _lock(repo: Path) -> threading.Lock:
    key = str(Path(repo).resolve()).lower()
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorktreeError(f"git {args[0]} failed: {e}") from e
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def _out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def root() -> Path:
    """Where session worktrees live: local_chat.worktree_root, else a short folder in the agent state folder (Windows path
    limits were hit with long ones on 2026-10-19)."""
    from bot import localchat

    configured = str(localchat.settings().get("worktree_root") or "").strip()
    if configured:
        p = Path(configured).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    from bot.agent_runtime.state import state_dir

    return state_dir("lcwt")


def repo_of(workspace: str) -> Optional[Path]:
    """The git repository a workspace is in, or None (no workspace, not a folder, not in a repository)."""
    if not workspace:
        return None
    p = Path(workspace).expanduser()
    if not p.is_dir():
        return None
    proc = _git(p, "rev-parse", "--show-toplevel", check=False)
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def _live_checkout() -> Optional[Path]:
    try:
        from bot.modules import registry

        return Path(registry.abp_root()).resolve()
    except Exception:  # noqa: BLE001
        return None


def _session(sid: str) -> dict:
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    return s


def _wt(sid: str, *, change: bool = True) -> tuple[dict, dict]:
    s = _session(sid)
    wt = s.get("worktree")
    if not wt:
        raise WorktreeError("this session has no worktree")
    if change and wt.get("worker"):
        raise WorktreeError("a Factory worker's worktree belongs to the conductor: it is read-only here")
    if not Path(wt["path"]).is_dir():
        raise WorktreeError(f"the worktree folder is gone ({wt['path']}); discard it or prune")
    return s, wt


def cwd_of(session: dict) -> str:
    """The folder a session's turn works in: its worktree (and the same sub-folder of the repository the workspace was in),
    else its workspace."""
    wt = session.get("worktree")
    if wt and Path(wt.get("path") or "").is_dir():
        return str(Path(wt["path"]) / (wt.get("rel") or ""))
    return str(session.get("workspace") or "")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:24].strip("-") or "session"


def create(sid: str, *, base: Optional[str] = None, name: Optional[str] = None) -> dict:
    """A worktree for the session on a new branch from `base`. The session's workspace must be in a git repository."""
    from bot import localchat

    s = _session(sid)
    if s.get("worktree"):
        raise WorktreeError("this session already has a worktree")
    repo = repo_of(s.get("workspace") or "")
    if repo is None:
        raise WorktreeError("a worktree needs the session's workspace to be inside a git repository")
    with _lock(repo):
        base = (base or "").strip() or _out(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if base == "HEAD":
            raise WorktreeError("the repository is on a detached HEAD: name the base branch")
        base_sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).stdout.strip()
        if not base_sha:
            raise WorktreeError(f"no branch or commit called {base!r} in {repo.name}")
        prefix = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(localchat.settings().get("worktree_prefix") or "chat")).strip("/") or "chat"
        branch = f"{prefix}/{_slug(name or s.get('title') or '')}-{sid[:6]}"
        folder = root() / f"{hashlib.sha1(str(repo).encode()).hexdigest()[:4]}{sid[:8]}"
        live = _live_checkout()
        if live is not None and folder.resolve() == live:
            raise WorktreeError("the live ABP checkout is never a session's worktree")
        if folder.exists():
            raise WorktreeError(f"{folder} already exists: prune first")
        _git(repo, "worktree", "add", "-b", branch, str(folder), base_sha)
    try:
        rel = str(Path(s["workspace"]).expanduser().resolve().relative_to(repo.resolve()))
    except ValueError:
        rel = ""
    wt = {"path": str(folder), "branch": branch, "base": base, "base_sha": base_sha, "repo": str(repo),
          "rel": "" if rel == "." else rel, "keep": False}
    store.set_worktree(sid, wt)
    store.add_event(sid, "worktree", action="created", branch=branch, base=base)
    return wt


def _dirty(path: Path) -> list[str]:
    return [ln[3:] for ln in _git(path, "status", "--porcelain", "--untracked-files=all").stdout.splitlines() if ln.strip()]


def _ahead_behind(path: Path, base: str) -> tuple[int, int]:
    proc = _git(path, "rev-list", "--left-right", "--count", f"{base}...HEAD", check=False)
    try:
        behind, ahead = (int(x) for x in proc.stdout.split())
    except ValueError:
        return 0, 0
    return ahead, behind


def status(sid: str) -> dict:
    s, wt = _wt(sid, change=False)
    path = Path(wt["path"])
    ahead, behind = _ahead_behind(path, wt["base"])
    dirty = _dirty(path)
    gitdir = Path(_out(path, "rev-parse", "--git-dir"))
    gitdir = gitdir if gitdir.is_absolute() else path / gitdir
    conflicts = _git(path, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    return {**wt, "head": _out(path, "rev-parse", "HEAD"), "ahead": ahead, "behind": behind, "dirty": dirty,
            "merging": (gitdir / "MERGE_HEAD").exists(), "conflicts": conflicts,
            "unmerged_work": bool(dirty) or ahead > 0}


def diff(sid: str) -> dict:
    """The session's changes against the point where it forked from the base: committed, uncommitted and new files."""
    s, wt = _wt(sid, change=False)
    path = Path(wt["path"])
    fork = _git(path, "merge-base", wt["base"], "HEAD", check=False).stdout.strip() or wt["base_sha"]
    untracked = _git(path, "ls-files", "--others", "--exclude-standard").stdout.split("\n")
    untracked = [u for u in untracked if u]
    text = _git(path, "diff", fork, "--").stdout
    for u in untracked[:50]:
        text += _git(path, "diff", "--no-index", "--", "/dev/null", u, check=False).stdout     # git knows /dev/null everywhere
    stat = _git(path, "diff", "--stat", fork, "--").stdout.strip()
    clipped = len(text) > DIFF_LIMIT
    return {"base": wt["base"], "fork_point": fork, "stat": stat, "untracked": untracked, "diff": text[:DIFF_LIMIT],
            "clipped": clipped}


def sync(sid: str) -> dict:
    """Merge the base branch into the session's branch. Refused with uncommitted changes (commit first: nothing in the tree is
    ever clobbered). On a conflict the merge is left in progress and the conflicted files are returned for the agent."""
    s, wt = _wt(sid)
    path = Path(wt["path"])
    with _lock(Path(wt["repo"])):
        dirty = _dirty(path)
        if dirty:
            raise WorktreeError(f"commit or discard the {len(dirty)} uncommitted change(s) first: syncing never overwrites work")
        before = _out(path, "rev-parse", "HEAD")
        proc = _git(path, *IDENT, "merge", "--no-edit", "--no-ff", wt["base"], check=False)
        conflicts = _git(path, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        after = _out(path, "rev-parse", "HEAD")
    if conflicts:
        store.add_event(sid, "worktree", action="sync_conflict", files=conflicts)
        return {"merged": False, "conflicts": conflicts, "message": (proc.stdout + proc.stderr).strip()[-2000:]}
    if proc.returncode != 0:
        raise WorktreeError(f"merging {wt['base']} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    store.add_event(sid, "worktree", action="synced", base=wt["base"])
    return {"merged": before != after, "conflicts": [], "head": after, "message": proc.stdout.strip()[-500:]}


def conflict_prompt(wt: dict, conflicts: list[str]) -> str:
    files = "\n".join(f"- {f}" for f in conflicts)
    return (f"Merging the base branch `{wt['base']}` into this session's branch `{wt['branch']}` stopped on conflicts in:\n{files}\n\n"
            "Resolve each conflict (keep both sides' intent), remove the conflict markers, then run the tests that cover them. "
            "Do not commit; I will review and commit.")


def draft_message(sid: str) -> str:
    """A commit message drafted from the changes (the owner edits or confirms it)."""
    s, wt = _wt(sid)
    path = Path(wt["path"])
    names = [ln[3:] for ln in _git(path, "status", "--porcelain").stdout.splitlines() if ln.strip()]
    title = " ".join(str(s.get("title") or "Local Chat session").split())[:68]
    body = "\n".join(f"- {n}" for n in names[:30]) + ("\n- ..." if len(names) > 30 else "")
    return f"{title}\n\nChanged in Local Chat session {sid}:\n{body}".strip()


def commit(sid: str, message: str) -> dict:
    message = (message or "").strip()
    if not message:
        raise WorktreeError("a commit needs a message")
    s, wt = _wt(sid)
    path = Path(wt["path"])
    with _lock(Path(wt["repo"])):
        if not _dirty(path):
            raise WorktreeError("nothing to commit")
        if _git(path, "diff", "--name-only", "--diff-filter=U", check=False).stdout.strip():
            raise WorktreeError("there are unresolved conflicts: resolve them first")
        _git(path, "add", "-A")
        _git(path, *IDENT, "commit", "-q", "-m", message)
        head = _out(path, "rev-parse", "HEAD")
    store.add_event(sid, "worktree", action="committed", head=head)
    return {"head": head, "message": message}


def _lost(path: Path, base: str) -> dict:
    dirty = _dirty(path)
    commits = _git(path, "log", "--oneline", f"{base}..HEAD", check=False).stdout.splitlines()
    return {"uncommitted": dirty, "commits": commits}


def discard(sid: str, *, confirm: bool = False) -> dict:
    """Remove the session's worktree (and its branch when nothing on it is unmerged). Unmerged work needs confirm=True."""
    s = _session(sid)
    wt = s.get("worktree")
    if not wt:
        raise WorktreeError("this session has no worktree")
    if wt.get("worker"):
        raise WorktreeError("a Factory worker's worktree belongs to the conductor: it is read-only here")
    path, repo = Path(wt["path"]), Path(wt["repo"])
    lost = _lost(path, wt["base"]) if path.is_dir() else {"uncommitted": [], "commits": []}
    if (lost["uncommitted"] or lost["commits"]) and not confirm:
        raise NeedsConfirmation(f"discarding loses {len(lost['uncommitted'])} uncommitted change(s) and {len(lost['commits'])} "
                                f"commit(s) not on {wt['base']}", lost)
    with _lock(repo):
        if path.is_dir():
            _git(repo, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(repo, "worktree", "prune", check=False)
        _git(repo, "branch", "-D", wt["branch"], check=False)
    store.set_worktree(sid, None)
    store.add_event(sid, "worktree", action="discarded", branch=wt["branch"], lost=lost)
    return {"discarded": True, "branch": wt["branch"], "lost": lost}


def set_keep(sid: str, keep: bool) -> dict:
    s, wt = _wt(sid)
    wt = {**wt, "keep": bool(keep)}
    store.set_worktree(sid, wt)
    return wt


def on_archive(sid: str) -> dict:
    """Archiving a session: an unchanged worktree (and its branch) goes; a changed or kept one stays until merged or discarded."""
    s = _session(sid)
    wt = s.get("worktree")
    if not wt:
        return {"worktree": None}
    if wt.get("keep"):
        return {"worktree": "kept", "why": "keep is on"}
    path = Path(wt["path"])
    if path.is_dir():
        lost = _lost(path, wt["base"])
        if lost["uncommitted"] or lost["commits"]:
            return {"worktree": "kept", "why": "it has unmerged work", "unmerged": lost}
    discard(sid, confirm=False)
    return {"worktree": "removed"}


def queue_merge(sid: str) -> dict:
    """The session's committed branch into the conductor's push queue, awaiting the owner's approval. Pushing happens only
    from there (bot/conductor/pusher.py), never from here."""
    from bot.conductor import store as cstore

    s, wt = _wt(sid)
    path = Path(wt["path"])
    st = status(sid)
    if st["dirty"]:
        raise WorktreeError("commit the uncommitted changes first; only committed work can be queued")
    if st["ahead"] <= 0:
        raise WorktreeError(f"nothing to merge: the branch has no commits that are not on {wt['base']}")
    head = _out(path, "rev-parse", "HEAD")
    entry = cstore.enqueue({"id": f"localchat-{sid}", "branch": wt["branch"], "head_sha": head, "base": wt["base"],
                            "repo": wt["repo"], "plan_id": "", "tasks": [], "log": f"Local Chat session {sid}: {s.get('title') or ''}"})
    store.add_event(sid, "worktree", action="queued", queue_id=entry.get("id"), head=head)
    return entry


def prune(*, clean_merged: bool = False) -> dict:
    """Remove orphan worktree folders (no session owns them) that have no uncommitted work, run `git worktree prune` in every
    known repository, and with clean_merged also remove the worktrees of sessions whose branch is merged into its base and
    whose tree is clean (unless keep is on). Branches with commits are never deleted here, except merged ones."""
    removed, kept, repos = [], [], set()
    owned = {}
    for s in store.sessions_with_worktrees():
        if not s["worktree"].get("path"):
            continue
        owned[str(Path(s["worktree"]["path"]).resolve()).lower()] = s
        if s["worktree"].get("repo"):
            repos.add(s["worktree"]["repo"])
    base = root()
    for folder in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        key = str(folder.resolve()).lower()
        if key in owned:
            continue
        top = _git(folder, "rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0:
            kept.append({"path": str(folder), "why": "not a git worktree; left alone"})
            continue
        if _dirty(folder):
            kept.append({"path": str(folder), "why": "it has uncommitted changes"})
            continue
        common = Path(_out(folder, "rev-parse", "--git-common-dir"))
        repo = (folder / common).resolve().parent if not common.is_absolute() else common.parent
        _git(repo, "worktree", "remove", "--force", str(folder), check=False)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        repos.add(str(repo))
        removed.append({"path": str(folder), "why": "orphan (no session)"})
    if clean_merged:
        for s in owned.values():
            wt = s["worktree"]
            path = Path(wt["path"])
            if wt.get("keep") or wt.get("worker") or not path.is_dir() or _dirty(path):
                continue
            merged = _git(path, "merge-base", "--is-ancestor", "HEAD", wt["base"], check=False).returncode == 0
            if merged:
                discard(s["id"], confirm=True)
                removed.append({"path": wt["path"], "why": f"merged into {wt['base']}", "session": s["id"]})
    for repo in repos:
        if Path(repo).is_dir():
            _git(Path(repo), "worktree", "prune", check=False)
    return {"removed": removed, "kept": kept}


def summary(session: dict) -> Optional[dict[str, Any]]:
    """The few worktree facts the sidebar shows, without failing on a missing folder."""
    wt = session.get("worktree")
    if not wt:
        return None
    try:
        st = status(session["id"])
        return {k: st[k] for k in ("branch", "base", "ahead", "behind", "dirty", "keep", "conflicts", "merging")}
    except (WorktreeError, KeyError) as e:
        return {"branch": wt.get("branch"), "base": wt.get("base"), "error": str(e)}
