"""A library of ready-made scripts in PowerShell, CMD, VBScript, Python and Bash, for the jobs that come up again and
again: system information, big files, ports, backups, cleanups, hashes, event logs, keeping a machine awake, waking
one over the network, PATH, scheduled tasks, downloads, duplicates, bulk renames, data conversion, and more.

Each script starts with a header (name, description, params, safety) that this module reads, so the list is always the
files themselves. ``safety`` is read, changes, executes or network; scripts that change something show what they would
do unless told to apply it.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, rel, run, which

group("scripts", "A library of PowerShell, CMD, VBScript, Python and Bash scripts: list, read, run, or copy into a project")

LIB = Path(__file__).parent / "scripts_lib"
LANG = {".ps1": "powershell", ".cmd": "cmd", ".bat": "cmd", ".vbs": "vbscript", ".py": "python", ".sh": "bash"}


def _header(p: Path) -> dict:
    text = p.read_text(encoding="utf-8", errors="replace")[:3000]
    meta = {"name": p.stem, "description": "", "params": "", "safety": "read"}
    for key in meta:
        m = re.search(rf"^\s*(?:#|rem|'|::)?\s*{key}:\s*(.+)$", text, re.M | re.I)
        if m:
            meta[key] = m.group(1).strip()
    return meta


def library() -> list[dict]:
    out = []
    for p in sorted(LIB.rglob("*")):
        if p.suffix.lower() in LANG and p.is_file():
            out.append({**_header(p), "language": LANG[p.suffix.lower()], "file": p.relative_to(LIB).as_posix()})
    return out


def _find(name: str) -> Path:
    matches = [p for p in LIB.rglob("*") if p.is_file() and p.suffix.lower() in LANG and (p.stem == name or p.name == name
                                                                                        or p.relative_to(LIB).as_posix() == name)]
    if not matches:
        raise ToolkitError(f"no script {name!r}; scripts.list shows them")
    if len(matches) > 1:
        # The same job in several languages: the one native to this system wins (PowerShell on Windows, Bash elsewhere).
        prefer = (".ps1", ".cmd", ".vbs", ".py", ".sh") if os.name == "nt" else (".sh", ".py", ".ps1")
        for ext in prefer:
            native = [p for p in matches if p.suffix.lower() == ext]
            if len(native) == 1:
                return native[0]
        raise ToolkitError(f"{name!r} is in several languages: " + ", ".join(p.relative_to(LIB).as_posix() for p in matches))
    return matches[0]


@action("scripts.list")
def list_scripts(language: str = "", query: str = "", safety: str = "") -> list:
    """The scripts in the library: name, language, what it does, its parameters and whether it changes anything

    language: powershell, cmd, vbscript, python or bash
    query: words to look for in the name or description
    safety: read, changes, executes or network
    """
    words = query.lower().split()
    return [s for s in library() if (not language or s["language"] == language) and (not safety or s["safety"] == safety)
            and all(w in f"{s['name']} {s['description']}".lower() for w in words)]


@action("scripts.show")
def show(name: str) -> dict:
    """A script's full source, with its header

    name: its name (or language/file.ext when the name exists in several languages)
    """
    p = _find(name)
    return {**_header(p), "language": LANG[p.suffix.lower()], "file": p.relative_to(LIB).as_posix(),
            "source": p.read_text(encoding="utf-8")}


def _command(p: Path, args: list[str]) -> list[str]:
    lang = LANG[p.suffix.lower()]
    if lang == "powershell":
        exe = which("pwsh") or which("powershell")
        if not exe:
            raise ToolkitError("PowerShell is not installed")
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(p), *args]
    if lang == "cmd":
        if os.name != "nt":
            raise ToolkitError("CMD scripts run on Windows only")
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(p), *args]
    if lang == "vbscript":
        if not which("cscript"):
            raise ToolkitError("VBScript runs on Windows only (cscript)")
        return [which("cscript"), "//nologo", str(p), *args]
    if lang == "python":
        return [sys.executable, "-X", "utf8", str(p), *args]
    if lang == "bash":
        if not which("bash"):
            raise ToolkitError("bash is not installed")
        return [which("bash"), str(p), *args]
    raise ToolkitError(f"cannot run {p.name}")


@action("scripts.run", executes=True)
def run_script(workspace: Path, name: str, args: Optional[list[str]] = None, timeout_s: float = 300, stdin: str = "") -> dict:
    """Run a script from the library in the working folder, with arguments

    name: the script's name
    args: its arguments (see its params)
    timeout_s: stop it after this many seconds
    """
    p = _find(name)
    with tempfile.TemporaryDirectory(prefix="abp-script-") as d:
        copy = Path(d) / p.name
        data = p.read_bytes()
        if p.suffix.lower() in (".cmd", ".bat", ".vbs"):
            # cmd.exe misreads labels in files with bare LF line endings, and cscript wants the ANSI code page.
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        if p.suffix.lower() == ".sh":
            data = data.replace(b"\r\n", b"\n")
        copy.write_bytes(data)
        r = run(_command(copy, list(args or [])), cwd=Path(workspace), timeout=min(timeout_s, 3600), input=stdin or None,
                env={"NO_COLOR": "1", "TERM": "dumb", "PYTHONIOENCODING": "utf-8"})   # plain text: no ANSI colours in the output
    return {"script": p.relative_to(LIB).as_posix(), "exit_code": r.code, "timed_out": r.timed_out,
            "stdout": r.out[-60_000:], "stderr": r.err[-20_000:]}


@action("scripts.install", writes=True)
def install(workspace: Path, name: str, folder: str = "scripts", overwrite: bool = False) -> dict:
    """Copy a script from the library into the project, so it can be edited and committed

    name: the script's name
    folder: where to put it inside the working folder
    """
    p = _find(name)
    dest = inside(workspace, str(Path(folder) / p.name))
    if dest.exists() and not overwrite:
        raise ToolkitError(f"{rel(workspace, dest)} already exists (overwrite=true replaces it)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = p.read_bytes()
    if p.suffix.lower() in (".cmd", ".bat", ".vbs", ".ps1"):
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    dest.write_bytes(data)
    if p.suffix.lower() in (".sh", ".py") and os.name != "nt":
        dest.chmod(0o755)
    return {"installed": rel(workspace, dest)}
