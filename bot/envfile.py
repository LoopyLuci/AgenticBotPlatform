"""Resolves which .env file to load secrets from, and lets the dashboard
edit its contents in place.

Locations, checked in order:
  1. an explicit path set via config/backends.yaml's `env_file` key
     (settable from the dashboard's Control Center -> Environment card)
  2. `<state root>/.env` (this install's own .env — see "Roots" below)
  3. ~/.claude/.env (a global .env shared with other Claude tooling) —
     NOT consulted when ABP_HOME is set: an embedded/sidecar deployment
     must never read another tool's secrets by accident.

Roots (two different things this module used to conflate):
  * CODE_ROOT  — where the running `bot` package, the desktop UI assets and
                 the bundled .venv live.
  * PROJECT_ROOT — the STATE root: `.env`, `config/`, `data/` (the database,
                 attachments, snapshots ...) and `logs/`. Kept under this
                 name because every module already imports it.
  By default both are the same directory. Set the ABP_HOME environment
  variable to keep all mutable state somewhere else — e.g. when ABP is a git
  submodule/sidecar of another server, so nothing is ever written inside the
  (possibly read-only) checkout and two hosts don't share one database.

Every write through write_content()/restore_backup() is preceded by a
timestamped copy into data/env_backups/ — nothing is ever overwritten
without one, and backups are never auto-pruned, so the edit history for
this file simply accumulates for as long as the install exists.

This module has no dependency on bot.config (which itself needs no env
vars to load) so it can run before anything else in bot/main.py.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _checkout_behind_build_output(package_root: Path) -> Optional[Path]:
    """`cargo tauri build` bundles a COPY of bot/ into
    <checkout>/desktop-app/src-tauri/target/<profile>/, a different folder
    from the source tree `python -m bot.main` runs against. A developer's
    built app and their source run should share one .env/config/database,
    or a value entered through one silently doesn't exist in the other. So
    when this package is running from such a build output INSIDE a source
    checkout, that checkout is the root. (This used to be a hardcoded
    developer path, `Z:\\Projects\\AgenticBotPlatform`, that took over on any
    machine where it happened to exist.)"""
    parts = package_root.parts
    if len(parts) >= 5 and parts[-2] == "target" and parts[-3] == "src-tauri" and parts[-4] == "desktop-app":
        checkout = package_root.parents[3]
        if (checkout / "bot" / "main.py").is_file():
            return checkout
    return None


def resolve_roots(package_root: Path, environ: Any) -> tuple[Path, Path, bool]:
    """(code_root, state_root, abp_home_active) for a package location and
    environment. Pure, so the rules are unit-testable."""
    code_root = _checkout_behind_build_output(package_root) or package_root
    home = (environ.get("ABP_HOME") or "").strip()
    if not home:
        return code_root, code_root, False
    state_root = Path(os.path.expandvars(home)).expanduser().resolve()
    return code_root, state_root, True


CODE_ROOT, PROJECT_ROOT, ABP_HOME_ACTIVE = resolve_roots(_PACKAGE_ROOT, os.environ)


def prepare_state_dir(state_root: Path, code_root: Path) -> None:
    """Make an ABP_HOME directory usable on first run: create the layout and
    seed the default routing config (the same job scripts/docker-entrypoint.sh
    does for a fresh Docker volume). Never overwrites anything that exists,
    and fails with a message that names the variable instead of a bare
    traceback from deep inside some later import."""
    try:
        for sub in ("config", "data", "logs"):
            (state_root / sub).mkdir(parents=True, exist_ok=True)
        target = state_root / "config" / "backends.yaml"
        default = code_root / "config" / "backends.yaml"
        if not target.exists() and default.is_file():
            shutil.copy2(default, target)
    except OSError as exc:
        raise SystemExit(f"ABP_HOME={state_root} is not usable ({exc}). Point it at a writable directory.") from exc


if ABP_HOME_ACTIVE:
    prepare_state_dir(PROJECT_ROOT, CODE_ROOT)

PROJECT_ENV = PROJECT_ROOT / ".env"
GLOBAL_ENV = Path.home() / ".claude" / ".env"
CONFIG_PATH = PROJECT_ROOT / "config" / "backends.yaml"
BACKUP_DIR = PROJECT_ROOT / "data" / "env_backups"


def stable_python_executable() -> str:
    """The python interpreter to hand to an EXTERNAL process we want to
    keep running independently of this app's own build/deploy cycle
    (a registered MCP server another program spawns and may keep alive
    across turns) — deliberately this project's own top-level `.venv`
    under CODE_ROOT, NOT sys.executable.

    sys.executable resolves to whichever interpreter happens to be
    running the CURRENT process, which for the actual running app is the
    Tauri-bundled copy under desktop-app/src-tauri/target/release/.venv
    — the exact directory `cargo tauri build` overwrites on every
    deploy. A long-lived external process still holding that
    interpreter's DLLs open (confirmed live: a Hermes agent's registered
    MCP server subprocess did exactly this) makes the next build fail
    with a Windows file-in-use error. CODE_ROOT/.venv is never a
    build target, so pointing there instead avoids the whole failure
    class. Falls back to sys.executable if that venv doesn't exist on
    this machine (e.g. a bare end-user install with no source checkout)."""
    rel = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    candidate = CODE_ROOT / ".venv" / rel[0] / rel[1]
    return str(candidate) if candidate.is_file() else sys.executable


def candidates() -> list[Path]:
    # With ABP_HOME set this is an isolated/embedded deployment: never fall
    # back to the global ~/.claude/.env that other tools share.
    return [PROJECT_ENV] if ABP_HOME_ACTIVE else [PROJECT_ENV, GLOBAL_ENV]


def configured_override() -> Optional[Path]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None
    raw = data.get("env_file")
    return Path(raw).expanduser() if raw else None


def resolve() -> Path:
    """First existing candidate wins: explicit override, then project .env,
    then the global ~/.claude/.env. If nothing exists yet, returns the
    project path anyway — python-dotenv silently no-ops on a missing file,
    so startup doesn't crash; /status and the dashboard will show missing
    secrets instead."""
    override = configured_override()
    if override:
        return override
    for c in candidates():
        if c.exists():
            return c
    return PROJECT_ENV


def status() -> dict:
    override = configured_override()
    resolved = resolve()
    return {
        "resolved_path": str(resolved),
        "resolved_exists": resolved.exists(),
        "override": str(override) if override else None,
        "candidates": [{"path": str(c), "exists": c.exists()} for c in candidates()],
    }


def backup_current() -> Optional[Path]:
    """Snapshot whatever's currently at the resolved .env path. Returns
    None (no-op) if there's nothing there yet to back up."""
    src = resolve()
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"env-{stamp}.env"
    n = 1
    while dest.exists():  # collision guard for saves within the same second
        dest = BACKUP_DIR / f"env-{stamp}-{n}.env"
        n += 1
    shutil.copy2(src, dest)
    return dest


def read_content() -> str:
    path = resolve()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def write_content(content: str, actor: str = "dashboard") -> Optional[Path]:
    """Back up whatever's there, then atomically replace it with `content`."""
    path = resolve()
    backup = backup_current()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

    try:
        from bot import db

        db.log_audit(
            actor=actor,
            action="env_edit",
            detail=f"wrote {path}" + (f" (backup: {backup.name})" if backup else " (no prior file)"),
        )
    except Exception:
        pass
    return backup


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("env-*.env"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = p.stat()
        out.append(
            {
                "name": p.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            }
        )
    return out


def _safe_backup_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", "..") or not name.startswith("env-"):
        raise ValueError(f"invalid backup name: {name!r}")
    candidate = BACKUP_DIR / name
    if candidate.resolve().parent != BACKUP_DIR.resolve():
        raise ValueError(f"invalid backup name: {name!r}")
    return candidate


def restore_backup(name: str, actor: str = "dashboard") -> Path:
    """Restore a named backup over the live .env — snapshotting the
    about-to-be-overwritten current version first, so a restore is itself
    always undoable."""
    candidate = _safe_backup_path(name)
    if not candidate.exists():
        raise FileNotFoundError(f"backup {name!r} not found")

    backup_current()
    dest = resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, dest)

    try:
        from bot import db

        db.log_audit(actor=actor, action="env_restore", detail=f"restored {name} -> {dest}")
    except Exception:
        pass
    return dest


def get_var(key: str) -> Optional[str]:
    """Reads a single KEY=value out of the resolved .env without going
    through python-dotenv, so callers (the Tauri shell's token auto-fill)
    can fetch one value with a plain subprocess call and no import cost."""
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$")
    for line in read_content().splitlines():
        m = pattern.match(line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def ensure_dashboard_token() -> str:
    """Guarantees DASHBOARD_TOKEN exists in .env, generating and persisting
    a fresh one the first time this ever runs on a given install — the
    manual-paste prompt in the dashboard UI should only ever be a fallback
    for someone editing .env by hand, never something a normal boot
    requires. Idempotent: a token already present is returned unchanged.
    Safe to call before bot.config/bot.db exist yet (a brand-new install's
    very first boot), since it only touches the .env file itself."""
    existing = get_var("DASHBOARD_TOKEN")
    if existing:
        return existing
    token = secrets.token_hex(24)
    content = read_content()
    # .env.example / the setup wizard ship a blank `DASHBOARD_TOKEN=`. Fill
    # THAT line in — appending a second one left the blank first line in
    # place, and get_var() (first match wins) kept reading "" on every boot,
    # generating and appending yet another token each time.
    blank = re.compile(r"^DASHBOARD_TOKEN=[ \t]*(?:\"\"|'')?[ \t]*\r?$", re.MULTILINE)
    if blank.search(content):
        content = blank.sub(f"DASHBOARD_TOKEN={token}", content, count=1)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"DASHBOARD_TOKEN={token}\n"
    write_content(content, actor="auto-generate")
    return token


if __name__ == "__main__":
    if "--print-token" in sys.argv:
        # Generates on first call, not just reads — the desktop app's Rust
        # get_dashboard_token command shells out to exactly this on every
        # boot, racing bot.main's own ensure_dashboard_token() call in the
        # spawned server process. A brand-new install has no .env yet, so a
        # plain read here could lose that race and print nothing, which is
        # what used to send the JS side straight to the manual-paste modal
        # on a fresh machine. ensure_dashboard_token() is idempotent (safe
        # for both processes to call), so whichever gets here first wins
        # and the other just reads back the same persisted value.
        print(ensure_dashboard_token())
    else:
        print(f"resolved .env: {resolve()}", file=sys.stderr)
