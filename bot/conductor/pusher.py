"""The pusher: pushes an approved integration branch to the remote's base branch - and does nothing else.

    entry = push(queue_id, by="owner")          # conductor.pusher.enabled must be true

It acts ONLY on a recorded approval in the push queue (who approved is in the entry, who pushed is in the audit trail). Before it
pushes it checks, in this order, and refuses (or marks the entry stale) on the first that fails:

1. the pusher is enabled (default off) and the entry is `approved` (not awaiting, rejected, pushed, stale or being pushed);
2. the branch tip is exactly the commit that was approved (`approved_sha`) - a branch that moved after approval is refused;
3. `git fetch`, then the remote's base branch is an ancestor of that commit; if it is not, the entry is marked **stale** and the
   integration has to be re-run (the pusher never merges, rebases or forces);
4. the leak guard (scripts/leak_guard.py) is clean on the exact tree of that commit (a detached worktree of the sha);
5. `git push <remote> <sha>:refs/heads/<base>` - the approved sha, never a branch name, never --force.

It never deploys: deploying the live ABP is a separate step for the owner. Every step and refusal is in the queue audit trail
(`GET /api/conductor/queue/{id}/audit`).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from bot.conductor import integrator, store


class PushRefused(RuntimeError):
    """The push did not happen and nothing was changed on the remote."""


class PushStale(PushRefused):
    """The remote moved on: the entry is marked stale and the integration must be re-run."""


def _leak_guard():
    path = Path(__file__).resolve().parents[2] / "scripts" / "leak_guard.py"
    if not path.is_file():
        raise PushRefused("scripts/leak_guard.py not found: the leak guard cannot run, so nothing is pushed")
    name = "conductor_pusher_leak_guard"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)        # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                       # type: ignore[union-attr]
    return mod


def leak_check(repo: Path, sha: str, private_dir: Optional[Path]) -> list[str]:
    """The leak guard over the exact tree of `sha` (a detached worktree of it, removed after). Lines of what fails the guard."""
    lg = _leak_guard()
    tmp = Path(tempfile.mkdtemp(prefix="abp-push-"))
    wt = tmp / "tree"
    try:
        integrator._git(repo, "worktree", "add", "-q", "--detach", str(wt), sha)
        try:
            cfg = lg.settings(private_dir)
            failing, _warn = lg.report(lg.scan(wt, private_dir=private_dir, allow=cfg["allow"]), cfg["modes"])
            return [str(f) for f in failing]
        finally:
            integrator._git(repo, "worktree", "remove", "--force", str(wt), check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push(queue_id: str, by: str, *, repo: Any = None, config: Optional[dict] = None, private_dir: Optional[Path] = None) -> dict:
    """Push the approved entry `queue_id`; returns the updated entry (status `pushed`). Raises PushRefused / PushStale."""
    if config is None:
        from bot import conductor

        config = conductor.settings()["pusher"]
    who = str(by or "").strip()
    if not who:
        raise PushRefused("say who pushes (by)")
    entry = store.get_queue_entry(queue_id)
    if entry is None:
        raise KeyError(queue_id)

    def refuse(why: str, cls=PushRefused):
        store.audit(queue_id, who, "push_refused", why)
        raise cls(why)

    if not config.get("enabled"):
        refuse("the pusher is disabled (conductor.pusher.enabled is false)")
    if entry["status"] != "approved":
        refuse(f"queue entry {queue_id} is {entry['status']}, not approved: only a recorded approval can be pushed")
    if not str(entry.get("decided_by") or "").strip():
        refuse("the approval does not say who approved it")
    sha = str(entry.get("approved_sha") or entry.get("head_sha") or "")
    if not sha or sha != entry.get("head_sha"):
        refuse("the approved commit and the queued commit differ")
    repo_path = Path(repo or entry.get("repo") or "")
    if not repo_path.is_dir():
        refuse("the repository of this integration is not available")
    remote = str(config.get("remote") or "origin")
    target = str(config.get("target") or entry.get("base") or "main")

    tip = integrator._git(repo_path, "rev-parse", "--verify", "-q", f"refs/heads/{entry['branch']}", check=False).stdout.strip()
    if tip != sha:
        refuse(f"branch {entry['branch']} is at {tip[:10] or 'nothing'}, not at the approved {sha[:10]}: it changed after the approval")

    if not store.claim_push(queue_id, who):
        refuse("another push of this entry is running, or it was just decided differently")
    try:
        fetch = integrator._git(repo_path, "fetch", "--quiet", remote, f"+refs/heads/{target}:refs/remotes/{remote}/{target}", check=False)
        if fetch.returncode != 0:
            raise PushRefused(f"could not fetch {remote}: {(fetch.stderr or fetch.stdout).strip()[:300]}")
        if integrator._git(repo_path, "merge-base", "--is-ancestor", f"refs/remotes/{remote}/{target}", sha, check=False).returncode != 0:
            store.finish_push(queue_id, who, "stale", f"{remote}/{target} is not an ancestor of {sha[:10]}: it moved on; re-run the integration")
            raise PushStale(f"{remote}/{target} moved on and is not an ancestor of {sha[:10]}: the entry is stale, re-run the integration")
        found = leak_check(repo_path, sha, private_dir or (Path(os.environ["ABP_PRIVATE_DIR"]) if os.environ.get("ABP_PRIVATE_DIR") else None))
        if found:
            raise PushRefused(f"the leak guard found {len(found)} problem(s) in the exact tree: {found[0]}"[:400])
        # The approved sha onto the target branch: a fast-forward only (the ancestor check above), never --force.
        res = integrator._git(repo_path, "push", remote, f"{sha}:refs/heads/{target}", check=False)
        if res.returncode != 0:
            err = (res.stderr or res.stdout).strip()
            if "non-fast-forward" in err or "fetch first" in err:
                store.finish_push(queue_id, who, "stale", f"the remote refused (it moved on): {err[:300]}")
                raise PushStale(f"{remote} refused the push (it moved on): the entry is stale, re-run the integration")
            raise PushRefused(f"git push failed: {err[:300]}")
    except PushStale:
        raise
    except PushRefused as exc:
        store.finish_push(queue_id, who, "approved", str(exc))     # back to approved: nothing was pushed, a later attempt may work
        raise
    except Exception as exc:  # noqa: BLE001 - leave the entry usable, say why
        store.finish_push(queue_id, who, "approved", f"{type(exc).__name__}: {exc}"[:300])
        raise PushRefused(f"push failed: {type(exc).__name__}: {exc}") from exc
    return store.finish_push(queue_id, who, "pushed", f"{sha} -> {remote}/{target}")
