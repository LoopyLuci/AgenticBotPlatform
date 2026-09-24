"""Builds the clean, filtered copy of bot/ and .venv/ that the desktop
installer bundles (see tauri.conf.json "bundle.resources" -> stage/...).

Bundling ../../.venv and ../../bot directly shipped, in every installer:
- dev-only packages that requirements-dev.txt says must never ship (pytest,
  pip-audit),
- __pycache__ directories, including ones left by pytest runs,
- the builder's absolute paths: pyvenv.cfg's `command =` line, the
  Scripts/activate* files, and the pip/pytest/... launcher .exes that embed
  the interpreter path — i.e. the developer's Windows username and project
  layout, published to every user.

This script copies only what the app needs, sanitizes pyvenv.cfg, and then
SCANS the result for personal paths, failing the build if any remain so the
leak can't quietly come back. It runs from tauri.conf.json's
beforeBuildCommand; you can also run it by hand.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "desktop-app" / "src-tauri" / "stage"

# Packages that are development/CI tooling (requirements-dev.txt), not runtime.
DEV_ONLY_PACKAGES = {"pytest", "_pytest", "pluggy", "iniconfig", "pip_audit"}
DEV_ONLY_DIST_PREFIXES = tuple(p.replace("_", "-") + "-" for p in DEV_ONLY_PACKAGES) + tuple(
    p + "-" for p in DEV_ONLY_PACKAGES
)
# Only these launchers are needed at runtime (the app runs `python -m ...`).
KEEP_SCRIPTS = {"python.exe", "pythonw.exe"}

TEXT_SUFFIXES = {".py", ".cfg", ".json", ".txt", ".yaml", ".yml", ".html", ".js", ".css", ".md", ".toml", ".pth", ".bat", ".ps1", ".ini", ""}


def _is_dev_dist_info(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith((".dist-info", ".egg-info")) and lowered.startswith(DEV_ONLY_DIST_PREFIXES)


def _venv_ignore(directory: str, names: list[str]) -> set[str]:
    rel = Path(directory).relative_to(ROOT / ".venv") if Path(directory) != ROOT / ".venv" else Path(".")
    ignored = {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}
    parts = rel.parts
    if parts == ("Scripts",):
        ignored |= {n for n in names if n not in KEEP_SCRIPTS}
    if parts == ("Lib", "site-packages"):
        ignored |= {n for n in names if n in DEV_ONLY_PACKAGES or _is_dev_dist_info(n)}
    return ignored


def _bot_ignore(_directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def sanitize_pyvenv_cfg(cfg: Path) -> None:
    """Drop the `command =` line (it records the builder's venv path); the
    Rust side rewrites home/executable for the real machine at first run."""
    kept = [ln for ln in cfg.read_text(encoding="utf-8").splitlines() if not ln.lower().startswith("command")]
    cfg.write_text("\n".join(kept) + "\n", encoding="utf-8")


def personal_markers() -> list[str]:
    """Strings that identify the person/machine that built this."""
    markers = {str(Path.home()), Path.home().as_posix(), "TelegramBotServer"}
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user and len(user) >= 4:
        markers.add(f"\\Users\\{user}")
        markers.add(f"/Users/{user}")
    return sorted(m for m in markers if m)


def scan_for_personal_paths(root: Path, markers: list[str]) -> list[tuple[Path, str]]:
    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.suffix.lower() != ".exe":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        # .exe launchers embed the interpreter path as bytes (UTF-8 / UTF-16).
        for marker in markers:
            if marker.encode("utf-8") in data or marker.encode("utf-16-le") in data:
                hits.append((path, marker))
                break
    return hits


def stage(stage_dir: Path = STAGE, markers: list[str] | None = None) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    shutil.copytree(ROOT / "bot", stage_dir / "bot", ignore=_bot_ignore)
    # The CI/CD telemetry package is a sibling of bot/ (standalone scripts and the CLI
    # import it without the bot package), so it ships beside it.
    if (ROOT / "abp_cicd").is_dir():
        shutil.copytree(ROOT / "abp_cicd", stage_dir / "abp_cicd", ignore=_bot_ignore)
    # bot/ssh_toolkit.py shells out to this submodule's own bin/ssh-toolkit.ps1 for
    # every CRUD operation (add/list/remove/visualize a connection, the peer-pairing
    # SSH auto-setup, ...) - only its "stream a command directly" path bypasses it
    # entirely. Without this, an installed app silently has none of that: is_available()
    # returns False and every dependent feature fails closed with no obvious cause
    # (a real gap this bundle never caught until a live two-machine peer-link test
    # surfaced it - see CHANGELOG's "Automatic SSH pairing" entry). Never ships the
    # submodule's own .git metadata - it's vendored content here, not a nested repo.
    vendor_dir = ROOT / "vendor" / "ssh_toolkit"
    if (vendor_dir / "bin" / "ssh-toolkit.ps1").is_file():
        shutil.copytree(
            vendor_dir, stage_dir / "vendor" / "ssh_toolkit",
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
    shutil.copytree(ROOT / ".venv", stage_dir / ".venv", ignore=_venv_ignore, symlinks=True)
    cfg = stage_dir / ".venv" / "pyvenv.cfg"
    if cfg.is_file():
        sanitize_pyvenv_cfg(cfg)
    (stage_dir / "config").mkdir()
    shutil.copy2(ROOT / "config" / "backends.yaml", stage_dir / "config" / "backends.yaml")

    hits = scan_for_personal_paths(stage_dir, markers if markers is not None else personal_markers())
    if hits:
        listing = "\n".join(f"  {p.relative_to(stage_dir)}  (contains {m!r})" for p, m in hits[:25])
        raise SystemExit(
            f"[FAILED] the installer bundle still contains the builder's personal paths "
            f"({len(hits)} file(s)):\n{listing}\nFix the staging filter before shipping."
        )
    size_mb = sum(f.stat().st_size for f in stage_dir.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"staged installer bundle at {stage_dir} ({size_mb:.0f} MB, no dev packages, no personal paths)")


if __name__ == "__main__":
    stage()
