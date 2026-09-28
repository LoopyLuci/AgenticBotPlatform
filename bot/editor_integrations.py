"""Editor integrations: the VS Code extension, and where editors find this ABP.

* `register_install()` records, at every server start, where this ABP's code, state and Python are in
  `~/.abp/install.json`. The VS Code extension reads it, so it finds a dev checkout or an installed
  copy with nothing to configure. The last ABP started wins. `ABP_INSTALL_POINTER` moves the file,
  which the tests do.
* `status()` / `install_vscode()` back the ABP Agents page's Editors tab. They detect VS Code's
  command-line tool, compare the bundled extension with the installed one, and install or update it
  with `code --install-extension`. That command changes only VS Code's own extension folder.

The extension package (`abp-vscode.vsix`) ships in the desktop installer's `integrations/` folder
(scripts/stage_bundle.py builds it). A dev checkout uses `integrations/vscode/dist/` after
`npm run package`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

EXTENSION_ID = "agenticbotplatform.abp-vscode"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EditorError(Exception):
    pass


def _roots() -> tuple[Path, Path]:
    from bot import envfile

    return Path(envfile.CODE_ROOT), Path(envfile.PROJECT_ROOT)


def pointer_path() -> Path:
    override = os.environ.get("ABP_INSTALL_POINTER", "").strip()
    return Path(override) if override else Path.home() / ".abp" / "install.json"


def acp_command() -> list[str]:
    """The command an ACP editor (Zed and others) runs to use this ABP."""
    return [sys.executable, "-m", "abp_acp", "--model", "auto"]


def register_install() -> Optional[Path]:
    """Record where this ABP is, for editors. Never raises: a read-only home must not stop the server."""
    code_root, state_root = _roots()
    if not (code_root / "abp_acp" / "__main__.py").is_file():
        return None                                   # nothing an editor could start here
    data = {"code_root": str(code_root), "state_root": str(state_root), "python": sys.executable,
            "pid": os.getpid()}
    target = pointer_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return target
    except OSError:
        return None


def bundled_vsix() -> Optional[Path]:
    code_root, _ = _roots()
    for candidate in (code_root / "integrations" / "abp-vscode.vsix",
                      code_root / "integrations" / "vscode" / "dist" / "abp-vscode.vsix"):
        if candidate.is_file():
            return candidate
    return None


def vsix_version(vsix: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(vsix) as z:
            return str(json.loads(z.read("extension/package.json")).get("version") or "") or None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def code_cli() -> Optional[str]:
    """VS Code's command-line tool: on PATH, or where the installers put it."""
    found = shutil.which("code")
    if found:
        return found
    candidates: list[Path] = []
    if os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if base:
                sub = "Programs/Microsoft VS Code" if base == os.environ.get("LOCALAPPDATA") else "Microsoft VS Code"
                candidates.append(Path(base) / sub / "bin" / "code.cmd")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"))
    else:
        candidates += [Path("/usr/bin/code"), Path("/snap/bin/code"), Path("/usr/share/code/bin/code")]
    return next((str(p) for p in candidates if p.is_file()), None)


def _run_code(cli: str, *args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
                          stdin=subprocess.DEVNULL)


def installed_version(cli: str) -> Optional[str]:
    try:
        r = _run_code(cli, "--list-extensions", "--show-versions", timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EditorError(f"could not ask VS Code for its extensions: {exc}") from exc
    for line in (r.stdout or "").splitlines():
        name, _, version = line.strip().partition("@")
        if name.lower() == EXTENSION_ID:
            return version or None
    return None


def _newer(a: Optional[str], b: Optional[str]) -> bool:
    """Is version `a` newer than `b` (dotted numbers; anything unparseable compares as older)?"""
    def parts(v: Optional[str]) -> tuple:
        try:
            return tuple(int(x) for x in (v or "").split("."))
        except ValueError:
            return ()
    return parts(a) > parts(b)


def status() -> dict:
    cli = code_cli()
    vsix = bundled_vsix()
    bundled = vsix_version(vsix) if vsix else None
    installed = None
    error = None
    if cli:
        try:
            installed = installed_version(cli)
        except EditorError as exc:
            error = str(exc)
    return {
        "vscode": {"cli": cli, "installed": installed, "bundled": bundled, "package": str(vsix) if vsix else None,
                   "update_available": bool(installed and bundled and _newer(bundled, installed)), "error": error},
        "acp_command": acp_command(),
        "pointer": str(pointer_path()),
    }


def install_vscode() -> dict:
    cli = code_cli()
    if not cli:
        raise EditorError("VS Code's command-line tool (code) was not found. Install VS Code, or add it to PATH "
                          "from VS Code's command palette: \"Shell Command: Install 'code' command in PATH\".")
    vsix = bundled_vsix()
    if not vsix:
        raise EditorError("this copy of ABP has no VS Code extension package. In a checkout, run `npm run package` "
                          "in integrations/vscode first.")
    register_install()
    try:
        r = _run_code(cli, "--install-extension", str(vsix), "--force", timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EditorError(f"VS Code could not install the extension: {exc}") from exc
    if r.returncode != 0:
        raise EditorError(f"VS Code could not install the extension: {(r.stderr or r.stdout).strip()[-800:]}")
    return status()
