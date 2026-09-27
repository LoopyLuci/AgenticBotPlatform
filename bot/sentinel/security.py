"""Security posture checks that run continuously, not once at install time.

Each check returns findings of the form {"key", "level", "message"}; the
Sentinel turns them into alerts. They look for the ways a correctly
installed ABP drifts into an unsafe state over months:

- the dashboard bound to a non-loopback address (it can run shell hooks and
  open terminals) without having been deliberately exposed;
- a weak or missing dashboard token;
- secret-bearing files (.env, vault.key, provider store, backups) readable by
  other users on POSIX systems;
- credentials that leaked into the log file in clear text;
- code that changed on disk outside a release, in an installed copy (a
  tampered or half-updated install). Skipped in a git checkout, where code
  changes are the point.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from bot.envfile import CODE_ROOT, PROJECT_ROOT
from bot.sentinel import journal

MANIFEST_PATH = journal.SENTINEL_DIR / "code-manifest.json"
LOG_TAIL_BYTES = 2 * 1024 * 1024
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}
# Shapes of well-known credentials; any of these in clear text in a log is a leak.
_LEAK_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-(?:proj-|or-v1-)?[A-Za-z0-9]{32,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),  # Telegram bot token
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _finding(key: str, level: str, message: str) -> dict[str, str]:
    return {"key": f"security.{key}", "level": level, "message": message}


def check_exposure() -> list[dict[str, str]]:
    out = []
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip()
    if host not in _LOOPBACK and os.environ.get("ABP_EXPOSE_DASHBOARD", "").lower() not in ("1", "true", "yes"):
        out.append(_finding("exposure", "warning",
                            f"the dashboard listens on {host}, reachable from other machines, and can run shell hooks and "
                            "terminals. Put an authenticating proxy or Tailscale in front, or set DASHBOARD_HOST=127.0.0.1. "
                            "(Set ABP_EXPOSE_DASHBOARD=1 to acknowledge a deliberate exposure.)"))
    token = os.environ.get("DASHBOARD_TOKEN", "")
    if len(token) < 32:
        out.append(_finding("token", "warning", "the dashboard token is missing or shorter than 32 characters"))
    return out


def _secret_paths() -> list[Path]:
    from bot import envfile

    paths = [Path(envfile.resolve()), PROJECT_ROOT / "data" / "vault.key", PROJECT_ROOT / "data" / "provider_store.db",
             PROJECT_ROOT / "config" / "providers.yaml", PROJECT_ROOT / "data" / "backups"]
    return [p for p in paths if p.exists()]


def check_permissions() -> list[dict[str, str]]:
    if os.name != "posix":
        return []  # Windows: files under the user profile inherit per-user ACLs
    out = []
    for p in _secret_paths():
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(_finding(f"perms:{p.name}", "warning",
                                f"{p} is accessible to other users (mode {stat.filemode(mode)}); it holds secrets"))
    return out


def tighten_permissions() -> list[str]:
    """The self-repair for check_permissions: owner-only access. POSIX only."""
    fixed = []
    if os.name != "posix":
        return fixed
    for p in _secret_paths():
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            p.chmod(0o700 if p.is_dir() else 0o600)
            fixed.append(str(p))
    return fixed


def check_log_leaks(log_path: Path) -> list[dict[str, str]]:
    if not log_path.is_file():
        return []
    size = log_path.stat().st_size
    with log_path.open("rb") as f:
        f.seek(max(0, size - LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    hits = sum(1 for pat in _LEAK_PATTERNS for _ in pat.finditer(text))
    if not hits:
        return []
    return [_finding("log-leak", "critical",
                     f"{hits} credential-shaped string(s) in clear text in {log_path.name}. Rotate the affected keys; "
                     "the Sentinel redacts the log file in place.")]


def redact_log(log_path: Path) -> int:
    """Rewrites the log with credential-shaped strings replaced. Returns the count."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    n = 0
    for pat in _LEAK_PATTERNS:
        text, k = pat.subn("[REDACTED]", text)
        n += k
    if n:
        tmp = log_path.with_suffix(log_path.suffix + ".redact")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(log_path)
    return n


# ------------------------------------------------------------ code integrity --

def _is_checkout() -> bool:
    return (CODE_ROOT / ".git").exists()


def _code_files() -> list[Path]:
    roots = [CODE_ROOT / "bot", CODE_ROOT / "abp_cicd"]
    return sorted(p for r in roots if r.exists() for p in r.rglob("*.py") if "__pycache__" not in p.parts)


def code_digest() -> dict[str, str]:
    return {str(p.relative_to(CODE_ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in _code_files()}


def check_code_integrity() -> list[dict[str, str]]:
    """First run of a version records a baseline; later runs of the SAME
    version compare against it. A release upgrade records a fresh baseline."""
    if _is_checkout():
        return []
    from bot import __version__

    current = code_digest()
    try:
        saved = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if not saved or saved.get("version") != __version__:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps({"version": __version__, "python": sys.version, "files": current}),
                                 encoding="utf-8")
        return []
    before = saved.get("files", {})
    changed = sorted(k for k in current.keys() & before.keys() if current[k] != before[k])
    added = sorted(current.keys() - before.keys())
    removed = sorted(before.keys() - current.keys())
    if not (changed or added or removed):
        return []
    detail = ", ".join((changed + added + removed)[:8])
    return [_finding("code-integrity", "critical",
                     f"ABP {__version__}'s code changed on disk outside a release ({len(changed)} changed, {len(added)} "
                     f"added, {len(removed)} removed: {detail}). If you did not do this, treat the install as "
                     "compromised and reinstall.")]


def run_all(log_path: "Path | str | None") -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for check in (check_exposure, check_permissions, check_code_integrity):
        try:
            findings.extend(check())
        except Exception as exc:  # noqa: BLE001 — one broken check must not silence the rest
            findings.append(_finding(f"check-failed:{check.__name__}", "info", f"{check.__name__} failed: {exc}"))
    if log_path:
        try:
            findings.extend(check_log_leaks(Path(log_path)))
        except OSError:
            pass
    return findings
