"""What every toolkit module shares: paths confined to the workspace, running programs, finding them, languages."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence

from abp_toolkit.registry import ToolkitError

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
             ".ruff_cache", ".pytest_cache", "dist", "build", "target", ".next", ".idea", ".vscode", "coverage",
             ".gradle", "bin", "obj", ".cache"}


def inside(workspace: Path, path: str | Path, *, must_exist: bool = False) -> Path:
    """`path` resolved against the workspace; refuses anything outside it."""
    root = Path(workspace).resolve()
    p = Path(path)
    full = (p if p.is_absolute() else root / p).resolve()
    if full != root and root not in full.parents:
        raise ToolkitError(f"{path} is outside the working folder ({root})")
    if must_exist and not full.exists():
        raise ToolkitError(f"{path} does not exist")
    return full


def rel(workspace: Path, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(workspace).resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def walk(root: Path, *, max_files: int = 20000, extensions: Optional[set[str]] = None) -> list[Path]:
    """Files under root (or root itself), skipping dependency and build folders."""
    root = Path(root)
    if root.is_file():
        return [root]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if extensions is None or p.suffix.lower() in extensions or p.name in extensions:
                out.append(p)
                if len(out) >= max_files:
                    return out
    return out


@dataclass
class Result:
    code: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def run(args: Sequence[str | Path], *, cwd: Optional[Path] = None, timeout: float = 120, input: Optional[str] = None,
        env: Optional[dict] = None) -> Result:
    """Run a program without a shell or console window; output decoded, never raises for a non-zero exit."""
    try:
        p = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, input=input, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                           env={**os.environ, **(env or {})}, stdin=None if input is not None else subprocess.DEVNULL)
        return Result(p.returncode, p.stdout or "", p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return Result(-1, out, f"timed out after {timeout:.0f}s", timed_out=True)
    except FileNotFoundError:
        raise ToolkitError(f"{args[0]} is not installed or not on PATH") from None


EXTRA_DIRS = [r"%ProgramFiles%\nodejs", r"%APPDATA%\npm", r"%USERPROFILE%\.cargo\bin", r"%USERPROFILE%\go\bin",
              r"%ProgramFiles%\Go\bin", r"%USERPROFILE%\scoop\shims", r"%ProgramData%\chocolatey\bin",
              r"%ProgramFiles%\Graphviz\bin", r"%ProgramFiles%\LLVM\bin", r"%ProgramFiles%\Git\bin",
              r"%LOCALAPPDATA%\Programs\Python\Python312\Scripts", "/usr/local/bin", "/opt/homebrew/bin"]


@lru_cache(maxsize=256)
def which(name: str) -> Optional[str]:
    """A program on PATH, in this interpreter's Scripts folder, or in the usual install folders."""
    found = shutil.which(name)
    if found:
        return found
    exe_dir = Path(sys.executable).parent
    for d in [exe_dir, exe_dir / "Scripts", *(Path(os.path.expandvars(x)) for x in EXTRA_DIRS)]:
        for candidate in (d / name, d / f"{name}.exe", d / f"{name}.cmd", d / f"{name}.bat"):
            if candidate.is_file():
                return str(candidate)
    return None


def python_module_available(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


LANGUAGES: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyw", ".pyi"),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"),
    "typescript": (".ts", ".tsx", ".mts", ".cts"),
    "json": (".json", ".jsonc", ".json5", ".webmanifest"),
    "yaml": (".yml", ".yaml"),
    "toml": (".toml",),
    "xml": (".xml", ".xsd", ".xsl", ".svg", ".plist", ".csproj", ".vbproj", ".props", ".targets", ".resx", ".xaml"),
    "html": (".html", ".htm"),
    "css": (".css", ".scss", ".sass", ".less"),
    "markdown": (".md", ".markdown"),
    "powershell": (".ps1", ".psm1", ".psd1"),
    "batch": (".bat", ".cmd"),
    "vbscript": (".vbs", ".vba", ".bas"),
    "shell": (".sh", ".bash", ".zsh", ".ksh"),
    "go": (".go",),
    "rust": (".rs",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ino"),
    "csharp": (".cs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "swift": (".swift",),
    "ruby": (".rb",),
    "php": (".php",),
    "lua": (".lua",),
    "perl": (".pl", ".pm"),
    "r": (".r", ".R"),
    "dart": (".dart",),
    "scala": (".scala",),
    "sql": (".sql",),
    "dockerfile": ("Dockerfile", ".dockerfile"),
    "ini": (".ini", ".cfg", ".conf", ".editorconfig", ".gitconfig"),
    "csv": (".csv", ".tsv"),
}
_BY_EXT = {ext.lower(): lang for lang, exts in LANGUAGES.items() for ext in exts}


def language_of(path: Path) -> Optional[str]:
    p = Path(path)
    if p.name in _BY_EXT:
        return _BY_EXT[p.name]
    if p.name.startswith("Dockerfile"):
        return "dockerfile"
    lang = _BY_EXT.get(p.suffix.lower())
    if lang:
        return lang
    if not p.suffix and p.is_file():
        try:
            head = p.open("rb").read(100)
        except OSError:
            return None
        if head.startswith(b"#!"):
            first = head.split(b"\n", 1)[0].decode("latin-1")
            for key, lang in (("python", "python"), ("node", "javascript"), ("bash", "shell"), ("sh", "shell"),
                              ("pwsh", "powershell"), ("ruby", "ruby"), ("perl", "perl")):
                if key in first:
                    return lang
    return None


def read_text(p: Path, limit: int = 20_000_000) -> str:
    data = Path(p).read_bytes()[:limit]
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def is_binary(p: Path) -> bool:
    try:
        chunk = Path(p).open("rb").read(4096)
    except OSError:
        return True
    return b"\x00" in chunk and not (chunk[:2] in (b"\xff\xfe", b"\xfe\xff"))


def first(items: Iterable, n: int) -> list:
    out = []
    for x in items:
        out.append(x)
        if len(out) >= n:
            break
    return out
