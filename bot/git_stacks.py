"""Git stacks: compose stacks whose definition lives in a git repo, deployed by ABP, with a poller that makes a push
deploy. This is the part of Portainer the Octopus estate depends on (octopus-ops/PORTAINER-EXIT.md), done so the
hazards that plan lists cannot happen:

- **A registry that stores desired state.** Each stack's repo, ref, compose file, flags and environment are stored
  here; a deploy writes exactly that. Nothing is echoed back from a running stack, so a deploy can never wipe the
  environment by leaving it out. The environment is sealed with ABP's vault key and never returned (only its
  variable names are).
- **Clones use this machine's own git credentials** (SSH key or credential helper); there is no stored clone token
  to be blanked.
- **The poller** (config `git_stacks:`): per-stack opt-in (`auto_deploy`), a global `paused` switch, `shadow` mode
  (decides and logs, deploys nothing: for a cutover), a debounce (a new commit must stay the head for
  `debounce_s`), and a commit that failed is not retried until the head moves again. Stacks listed in
  `self_hosting` deploy after the others. Every decision and failure is an event (and an audit entry), so a push
  that did not deploy is visible, not silent.

Deploys run `docker compose -p <name> -f <compose file> --env-file <sealed env, written 0600 for the call> up -d
--remove-orphans`, with `--pull always` when asked, through bot/docker_mgr.py's validated runner.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot import db, docker_mgr as dk

logger = logging.getLogger("bot.git_stacks")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,199}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPO = re.compile(r"^(https://[^\s'\"]+|ssh://[^\s'\"]+|git@[A-Za-z0-9.-]+:[^\s'\"]+|/[^\s'\"]+|[A-Za-z]:[\\/][^'\"]+)$")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_deploy_lock = threading.Lock()


class StackError(Exception):
    pass


def _conn():
    c = db.get_conn()
    c.execute("""CREATE TABLE IF NOT EXISTS git_stacks (
        name TEXT PRIMARY KEY, repo TEXT NOT NULL, ref TEXT NOT NULL DEFAULT 'main',
        compose_file TEXT NOT NULL DEFAULT 'docker-compose.yml', auto_deploy INTEGER NOT NULL DEFAULT 0,
        pull INTEGER NOT NULL DEFAULT 0, env_sealed TEXT NOT NULL DEFAULT '', env_keys TEXT NOT NULL DEFAULT '[]',
        deployed_commit TEXT, deployed_at REAL, failed_commit TEXT, last_error TEXT,
        pending_commit TEXT, pending_since REAL, remote_commit TEXT, checked_at REAL, created_at REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS git_stack_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, stack TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT)""")
    return c


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("git_stacks") or {})
    except Exception:  # noqa: BLE001
        return {}


def settings() -> dict:
    c = _cfg()
    return {"poll_interval_s": int(c.get("poll_interval_s") or 120), "paused": bool(c.get("paused", False)),
            "shadow": bool(c.get("shadow", False)), "debounce_s": int(c.get("debounce_s") if c.get("debounce_s") is not None else 60),
            "self_hosting": list(c.get("self_hosting") or [])}


def event(stack: str, kind: str, detail: str = "") -> None:
    c = _conn()
    with db._lock:
        c.execute("INSERT INTO git_stack_events (at, stack, kind, detail) VALUES (?, ?, ?, ?)", (time.time(), stack, kind, detail[:2000]))
        c.execute("DELETE FROM git_stack_events WHERE id < (SELECT MAX(id) - 2000 FROM git_stack_events)")
        c.commit()
    if kind in ("deploy_failed", "check_failed"):
        logger.warning("git stack %s: %s: %s", stack, kind, detail[:300])
        db.log_audit(actor="git-stacks", action=f"git_stack_{kind}", detail=f"{stack}: {detail[:250]}")


def events(limit: int = 100, stack: Optional[str] = None) -> list[dict]:
    q = "SELECT at, stack, kind, detail FROM git_stack_events" + (" WHERE stack = ?" if stack else "") + " ORDER BY id DESC LIMIT ?"
    rows = _conn().execute(q, ((stack, limit) if stack else (limit,))).fetchall()
    return [dict(r) for r in rows]


# ---- the registry --------------------------------------------------------------------------------------------

def _row(name: str):
    r = _conn().execute("SELECT * FROM git_stacks WHERE name = ?", (name,)).fetchone()
    if r is None:
        raise StackError(f"no git stack {name!r}")
    return r


def _public(r) -> dict:
    d = {k: r[k] for k in r.keys() if k != "env_sealed"}
    d["env_keys"] = json.loads(r["env_keys"] or "[]")
    d["auto_deploy"], d["pull"] = bool(r["auto_deploy"]), bool(r["pull"])
    d["behind"] = bool(r["remote_commit"] and r["deployed_commit"] and r["remote_commit"] != r["deployed_commit"])
    return d


def _check(repo: str, ref: str, compose_file: str) -> None:
    if not _REPO.match(repo or ""):
        raise StackError("repo must be an https://, ssh:// or git@host: URL, or a local path")
    if not _REF.match(ref or "") or ".." in ref:
        raise StackError("bad ref")
    if not _PATH.match(compose_file or "") or ".." in compose_file:
        raise StackError("compose_file must be a relative path inside the repo")


def _seal_env(env: dict) -> tuple[str, str]:
    from bot import vault
    clean = {}
    for k, v in (env or {}).items():
        if not _ENV_KEY.match(str(k)) or "\n" in str(v) or "\r" in str(v):
            raise StackError(f"bad env entry {k!r}")
        clean[str(k)] = str(v)
    return (vault.seal(json.dumps(clean)) if clean else ""), json.dumps(sorted(clean))


def _env(r) -> dict:
    if not r["env_sealed"]:
        return {}
    from bot import vault
    return json.loads(vault.unseal(r["env_sealed"]))


def add(name: str, repo: str, ref: str = "main", compose_file: str = "docker-compose.yml", *, env: Optional[dict] = None,
        auto_deploy: bool = False, pull: bool = False) -> dict:
    name = dk._stack_name(name)
    _check(repo, ref, compose_file)
    sealed, keys = _seal_env(env or {})
    c = _conn()
    with db._lock:
        try:
            c.execute("INSERT INTO git_stacks (name, repo, ref, compose_file, auto_deploy, pull, env_sealed, env_keys, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, repo, ref, compose_file, int(auto_deploy), int(pull), sealed, keys, time.time()))
        except Exception as e:  # noqa: BLE001 - sqlite IntegrityError
            raise StackError(f"a git stack named {name!r} exists already") from e
        c.commit()
    event(name, "added", f"{repo} {ref} {compose_file}")
    return get(name)


def update(name: str, **fields: Any) -> dict:
    r = _row(name)
    repo, ref, cf = fields.get("repo", r["repo"]), fields.get("ref", r["ref"]), fields.get("compose_file", r["compose_file"])
    _check(repo, ref, cf)
    sets = {"repo": repo, "ref": ref, "compose_file": cf}
    for flag in ("auto_deploy", "pull"):
        if flag in fields:
            sets[flag] = int(bool(fields[flag]))
    if "env" in fields:   # the whole desired environment, replaced
        sets["env_sealed"], sets["env_keys"] = _seal_env(fields["env"] or {})
    if ref != r["ref"] or repo != r["repo"]:
        sets.update(failed_commit=None, pending_commit=None, pending_since=None)
    c = _conn()
    with db._lock:
        c.execute(f"UPDATE git_stacks SET {', '.join(f'{k} = ?' for k in sets)} WHERE name = ?", (*sets.values(), name))
        c.commit()
    event(name, "updated", ", ".join(k if k != "env_sealed" else "env" for k in sets if k != "env_keys"))
    return get(name)


def remove(name: str, *, down: bool = False) -> dict:
    _row(name)
    out = dk.stack_action(name, "down") if down else None
    c = _conn()
    with db._lock:
        c.execute("DELETE FROM git_stacks WHERE name = ?", (name,))
        c.commit()
    event(name, "removed", "and brought down" if down else "")
    return {"removed": name, "down": out}


def get(name: str) -> dict:
    return _public(_row(name))


def listing() -> list[dict]:
    return [_public(r) for r in _conn().execute("SELECT * FROM git_stacks ORDER BY name").fetchall()]


# ---- git and deploys -----------------------------------------------------------------------------------------

def _git(args: list[str], cwd: Optional[Path] = None, timeout: int = 300) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
                       creationflags=NO_WINDOW, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if p.returncode != 0:
        raise StackError(f"git {args[0]}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout


def remote_head(name: str) -> str:
    r = _row(name)
    out = _git(["ls-remote", r["repo"], r["ref"]], timeout=60).split()
    if not out:
        raise StackError(f"{r['repo']} has no ref {r['ref']}")
    sha = out[0]
    c = _conn()
    with db._lock:
        c.execute("UPDATE git_stacks SET remote_commit = ?, checked_at = ? WHERE name = ?", (sha, time.time(), name))
        c.commit()
    return sha


def _checkout(r, commit: Optional[str]) -> tuple[Path, str]:
    src = dk._stack_dir(r["name"]) / "src"
    if not (src / ".git").is_dir():
        src.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "-q", "--no-checkout", r["repo"], str(src)], timeout=900)
    _git(["fetch", "-q", "--prune", "origin", r["ref"]], cwd=src, timeout=600)
    target = commit or _git(["rev-parse", "FETCH_HEAD"], cwd=src).strip()
    _git(["checkout", "-q", "--force", "--detach", target], cwd=src)
    _git(["clean", "-q", "-fdx", "-e", ".abp.env"], cwd=src)
    return src, _git(["rev-parse", "HEAD"], cwd=src).strip()


def deploy(name: str, *, pull: Optional[bool] = None, commit: Optional[str] = None, reason: str = "manual") -> dict:
    """Deploy the stack's ref (or one commit) exactly as stored. Returns {ok, commit, output}."""
    r = _row(name)
    with _deploy_lock:
        env_file = None
        try:
            src, sha = _checkout(r, commit)
            compose = src / r["compose_file"]
            if not compose.is_file():
                raise StackError(f"{r['compose_file']} is not in {r['repo']} at {sha[:7]}")
            env_file = src / ".abp.env"
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(f"{k}={v}\n" for k, v in _env(r).items()))
            args = ["compose", "-p", name, "-f", str(compose), "--env-file", str(env_file), "up", "-d", "--remove-orphans"]
            if pull if pull is not None else r["pull"]:
                args += ["--pull", "always"]
            ok, out = dk._run(args, timeout=1800, cwd=str(compose.parent))
        except (StackError, OSError, subprocess.TimeoutExpired) as e:
            ok, out, sha = False, str(e), commit or r["remote_commit"] or ""
        finally:
            if env_file is not None:
                env_file.unlink(missing_ok=True)   # the sealed store is the only copy at rest
        c = _conn()
        with db._lock:
            if ok:
                c.execute("UPDATE git_stacks SET deployed_commit = ?, deployed_at = ?, failed_commit = NULL, last_error = NULL, "
                          "pending_commit = NULL, pending_since = NULL WHERE name = ?", (sha, time.time(), name))
            else:
                c.execute("UPDATE git_stacks SET failed_commit = ?, last_error = ?, pending_commit = NULL, pending_since = NULL "
                          "WHERE name = ?", (sha, out[-2000:], name))
            c.commit()
        event(name, "deployed" if ok else "deploy_failed", f"{sha[:12]} ({reason}){'' if ok else ': ' + out[-600:]}")
        return {"ok": ok, "commit": sha, "output": dk.redact(out) if hasattr(dk, "redact") else out}


# ---- the poller ----------------------------------------------------------------------------------------------

def poll_once(now: Optional[float] = None) -> list[dict]:
    """One pass over the auto-deploy stacks. Returns the decisions made."""
    s = settings()
    now = now or time.time()
    decisions: list[dict] = []
    if s["paused"]:
        return [{"stack": "*", "decision": "paused"}]
    rows = _conn().execute("SELECT * FROM git_stacks WHERE auto_deploy = 1").fetchall()
    rows = sorted(rows, key=lambda r: (r["name"] in s["self_hosting"], r["name"]))   # self-hosting stacks last
    for r in rows:
        name = r["name"]
        try:
            head = remote_head(name)
        except StackError as e:
            event(name, "check_failed", str(e))
            decisions.append({"stack": name, "decision": "check_failed", "error": str(e)})
            continue
        if head == r["deployed_commit"]:
            decisions.append({"stack": name, "decision": "current", "commit": head})
            continue
        if head == r["failed_commit"]:
            decisions.append({"stack": name, "decision": "skip_failed", "commit": head})
            continue
        c = _conn()
        if head != r["pending_commit"]:
            with db._lock:
                c.execute("UPDATE git_stacks SET pending_commit = ?, pending_since = ? WHERE name = ?", (head, now, name))
                c.commit()
            event(name, "new_commit", head[:12])
            if s["debounce_s"] > 0:
                decisions.append({"stack": name, "decision": "debounce", "commit": head})
                continue
        elif now - float(r["pending_since"] or now) < s["debounce_s"]:
            decisions.append({"stack": name, "decision": "debounce", "commit": head})
            continue
        if s["shadow"]:
            event(name, "would_deploy", head[:12])
            decisions.append({"stack": name, "decision": "would_deploy", "commit": head})
            continue
        res = deploy(name, commit=head, reason="poller")
        decisions.append({"stack": name, "decision": "deployed" if res["ok"] else "deploy_failed", "commit": head})
    return decisions


def poller_forever(stop: threading.Event) -> None:
    """Runs in a background thread from bot/main.py. Sleeps while there is nothing to poll."""
    while not stop.is_set():
        interval = max(30, settings()["poll_interval_s"])
        try:
            if _conn().execute("SELECT 1 FROM git_stacks WHERE auto_deploy = 1 LIMIT 1").fetchone():
                poll_once()
        except Exception as e:  # noqa: BLE001 - the poller never dies
            logger.warning("git stack poller: %s", e)
        stop.wait(interval)
