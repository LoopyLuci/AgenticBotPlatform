"""Builds the clean, filtered copy of bot/ and .venv/ that the desktop
installer bundles (see tauri.conf.json "bundle.resources" -> stage/...).

Bundling ../../.venv and ../../bot directly shipped, in every installer:
- dev-only packages that requirements-dev.txt says must never ship
  (pip-audit; pytest ships since the Factory runs tests with it),
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

import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "desktop-app" / "src-tauri" / "stage"
# Stamped next to the staged code and shipped as a bundle resource, so the
# running app can always answer "which commit is this?" - see
# bot/diagnostics.py's build_status() and scripts/deploy_local.py.
BUILD_STAMP = ".abp_build.json"

# Packages that are development/CI tooling (requirements-dev.txt), not runtime. pytest (and its pluggy, iniconfig) is runtime now:
# a Factory task's proof runs pytest with ABP's interpreter when the repository has no venv (bot/conductor/confine.py).
DEV_ONLY_PACKAGES = {"pip_audit"}
DEV_ONLY_DIST_PREFIXES = tuple(p.replace("_", "-") + "-" for p in DEV_ONLY_PACKAGES) + tuple(
    p + "-" for p in DEV_ONLY_PACKAGES
)
# Only these launchers are needed at runtime (the app runs `python -m ...`).
KEEP_SCRIPTS = {"python.exe", "pythonw.exe"}

TEXT_SUFFIXES = {".py", ".cfg", ".json", ".txt", ".yaml", ".yml", ".html", ".js", ".css", ".md", ".toml", ".pth", ".bat", ".ps1", ".ini", ""}


def shipped_config(root: Path) -> bytes:
    """The routing config an installer ships: the committed config/backends.yaml, not the builder's working copy.

    In a developer's checkout that file also holds their own live settings (a linked skill folder, a chosen model),
    which must never reach anyone else's install. Outside a git checkout (a source tarball) the file as it is."""
    try:
        out = subprocess.run(["git", "show", "HEAD:config/backends.yaml"], cwd=root, capture_output=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return (root / "config" / "backends.yaml").read_bytes()


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


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def stage_native(stage_venv: Path, root: Path | None = None, log=print) -> list[str]:
    """Build each native/<crate> extension from this tree and install it into the STAGED venv.

    Copying the developer's .venv ships whatever build happens to be installed there - possibly an older one, or
    a `maturin develop` install whose metadata points back at this checkout. Instead every crate's release wheel is
    built with the developer's interpreter and unpacked into the staged site-packages (a wheel is a zip laid out
    for site-packages), replacing the copied install. Nothing has the staged venv open, so this never fights a
    running ABP or test for a locked .pyd. Returns the crates installed.

    Without cargo the copied builds ship as they are (and ABP's Python fallbacks cover a missing one);
    ABP_STAGE_NATIVE=0 skips the builds for a quick restage."""
    import tempfile
    import zipfile

    root = ROOT if root is None else root
    crates = sorted(p.parent for p in (root / "native").glob("*/pyproject.toml"))
    if not crates:
        return []
    if os.environ.get("ABP_STAGE_NATIVE", "1").strip() == "0":
        log("native extensions: not rebuilt (ABP_STAGE_NATIVE=0) - shipping the copies from .venv")
        return []
    if not shutil.which("cargo"):
        log("native extensions: cargo not on PATH - shipping the copies from .venv")
        return []
    builder = _venv_python(root / ".venv")
    found = [stage_venv / "Lib" / "site-packages"] if os.name == "nt" else sorted(stage_venv.glob("lib/python3*/site-packages"))
    site = found[0] if found and found[0].is_dir() else None
    if site is None:
        raise SystemExit(f"[FAILED] no site-packages in the staged venv {stage_venv}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    installed = []
    with tempfile.TemporaryDirectory(prefix="abp-native-wheels-") as tmp:
        for crate in crates:
            out = Path(tmp) / crate.name
            log(f"native extensions: building {crate.name}")
            env = {**os.environ, "PYO3_PYTHON": str(builder)}
            done = subprocess.run([str(builder), "-m", "maturin", "build", "--release", "--out", str(out)],
                                  cwd=str(crate), env=env, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", creationflags=flags)
            wheels = sorted(out.glob("*.whl"))
            if done.returncode != 0 or not wheels:
                raise SystemExit(f"[FAILED] building native/{crate.name}:\n{(done.stderr or done.stdout)[-3000:]}")
            with zipfile.ZipFile(wheels[-1]) as whl:
                tops = {n.split("/", 1)[0] for n in whl.namelist()}
                dist = next(t for t in tops if t.endswith(".dist-info"))
                project = dist.split("-", 1)[0].lower()
                # Drop the copied install of this project (any version, editable or not) before unpacking.
                for old in site.iterdir():
                    if old.name.endswith(".dist-info") and old.name.split("-", 1)[0].lower() == project:
                        shutil.rmtree(old)
                for top in tops:
                    target = site / top
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                whl.extractall(site)
            installed.append(crate.name)
    log(f"native extensions: {', '.join(installed)} built from this tree into the bundle")
    return installed


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


def stage_vscode_extension(target: Path) -> None:
    """Put the VS Code extension package in the bundle, so ABP can install it with one click (the ABP
    Agents page's Editors tab). The folder always exists, since the installer bundles it as a resource; it
    only lacks the package when npm is not installed, and ABP then says how to build it."""
    import subprocess

    target.mkdir(parents=True, exist_ok=True)
    (target / "README.txt").write_text("abp-vscode.vsix: ABP's VS Code extension. Install it from the ABP Agents page "
                                       "(Editors tab) or with: code --install-extension abp-vscode.vsix\n", encoding="utf-8")
    ext = ROOT / "integrations" / "vscode"
    npm = shutil.which("npm")
    if not npm or not (ext / "package.json").is_file():
        print("[warn] npm not found: the installer will not include the VS Code extension")
        return
    steps = ([] if (ext / "node_modules").is_dir() else [[npm, "ci", "--no-audit", "--no-fund"]]) + [[npm, "run", "package"]]
    for cmd in steps:
        r = subprocess.run(cmd, cwd=ext, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise SystemExit(f"[FAILED] building the VS Code extension ({' '.join(cmd[1:])}):\n{(r.stdout + r.stderr)[-3000:]}")
    shutil.copy2(ext / "dist" / "abp-vscode.vsix", target / "abp-vscode.vsix")


def _git(*args: str) -> str:
    """One git query, never fatal: a missing git, a hang, a non-checkout
    directory all mean "no answer", which the stamp records honestly as an
    empty value rather than failing a build over."""
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def write_build_stamp(stage_dir: Path = STAGE) -> Path:
    """Records WHICH commit this bundle was built from, beside the staged
    code, so a running app can compare it against the checkout it was
    started from.

    This exists because a release build runs the Python copied next to its
    own exe (desktop-app/src-tauri/src/lib.rs's resolve_paths), not the
    source tree - so "the running app is serving code from four days ago"
    is possible, invisible (git says HEAD is current), and only visible by
    asking the running process what it actually loaded. Empty values are
    honest unknowns: a source tarball, or a checkout that has no git, simply
    has no commit to report.

    Deliberately NOT a build time only: the stamp is rewritten on every
    `cargo tauri build` (this script is its beforeBuildCommand), so it
    describes the code in the folder, not the machine."""
    stamp = {
        "commit": _git("rev-parse", "HEAD"),
        "commit_date": _git("log", "-1", "--format=%cs"),
        "dirty": bool(_git("status", "--porcelain", "--", "bot", "abp_cicd", "abp_run", "abp_acp", "abp_agenteval",
                           "abp_toolkit", "abp_modkit", "abp_native", "catalog", "config/backends.yaml")),
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    path = Path(stage_dir) / BUILD_STAMP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def stage(stage_dir: Path = STAGE, markers: list[str] | None = None) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    shutil.copytree(ROOT / "bot", stage_dir / "bot", ignore=_bot_ignore)
    # The CI/CD telemetry package is a sibling of bot/ (standalone scripts and the CLI
    # import it without the bot package), so it ships beside it.
    if (ROOT / "abp_cicd").is_dir():
        shutil.copytree(ROOT / "abp_cicd", stage_dir / "abp_cicd", ignore=_bot_ignore)
    # Editors (the VS Code extension, Zed and other ACP clients) start `python -m abp_acp` in
    # the installed folder; it runs turns through abp_run, and its scripted test mode uses
    # abp_agenteval's replay transport. The agent's toolkit_* tools come from abp_toolkit. abp_modkit is what makes
    # a project a module (the adopt routes import it, and adopted modules' hubs run `python -m abp_modkit`).
    # abp_native is the pure-Python face of the Rust accelerators: core modules import it, and the compiled modules
    # it uses (and abp_wasm, abp_rules) ship inside .venv, so a machine without them runs the Python fallbacks.
    for pkg in ("abp_run", "abp_acp", "abp_agenteval", "abp_toolkit", "abp_modkit", "abp_native"):
        if (ROOT / pkg).is_dir():
            shutil.copytree(ROOT / pkg, stage_dir / pkg, ignore=_bot_ignore)
    # The module catalog: overlays ABP ships for third-party repos (bot/modules/registry.py reads <root>/catalog).
    if (ROOT / "catalog").is_dir():
        shutil.copytree(ROOT / "catalog", stage_dir / "catalog", ignore=_bot_ignore)
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
    stage_vscode_extension(stage_dir / "integrations")
    shutil.copytree(ROOT / ".venv", stage_dir / ".venv", ignore=_venv_ignore, symlinks=True)
    cfg = stage_dir / ".venv" / "pyvenv.cfg"
    if cfg.is_file():
        sanitize_pyvenv_cfg(cfg)
    stage_native(stage_dir / ".venv")
    (stage_dir / "config").mkdir()
    (stage_dir / "config" / "backends.yaml").write_bytes(shipped_config(ROOT))
    stamp = write_build_stamp(stage_dir)
    print(f"build stamp: {json.loads(stamp.read_text(encoding='utf-8'))['commit'] or 'no commit (not a checkout)'}")

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
