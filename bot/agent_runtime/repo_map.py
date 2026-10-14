"""repo_map: a compact outline of a codebase (roadmap P3).

For a model to work in an unfamiliar repository it helps to see the shape first: which
files exist and what each defines. `repo_map` returns files with their classes and
functions, most-referenced files first, trimmed to a token budget - a few thousand tokens
instead of reading files one by one.

How it works, and its limits:

* **Python** is parsed with the standard `ast` module (exact: classes, functions, methods,
  with their argument lists).
* **JavaScript / TypeScript, Go, Rust, Java, Kotlin, C#, Swift, Ruby, PHP** are read with
  per-language regular expressions for declarations. That is a heuristic, not a parser:
  it finds ordinary declarations at the start of a line and will miss unusual formatting.
  A tree-sitter based version would be more exact; it is not built (the package is not
  installed).
* **Ranking** counts, for each symbol a file defines, how many *other* files mention it by
  name, so central files come first. It is a proxy for importance, not a call graph.
* Inside a git checkout the file list comes from `git ls-files` (so .gitignore'd build output is never walked);
  otherwise vendored/hidden folders are skipped. Files over 300 KB are skipped. A file-count/time budget bounds
  the scan and the output says when it truncated. The tool runs off the event loop (asyncio.to_thread).
* Results are cached per folder until a file changes.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import time
from collections import Counter, OrderedDict
from pathlib import Path

from bot.agent_runtime import toolspec
from bot.agent_runtime.coding_tools import SKIP_DIRS
from bot.agent_runtime.errors import ToolError, safe_path

MAX_FILE_BYTES = 300_000
MAX_FILES = 4000
TIME_BUDGET_S = 20.0
DEFAULT_TOKENS = 2000
MAX_TOKENS = 12000
CHARS_PER_TOKEN = 3.5

_C_LIKE = {
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".go": "go", ".rs": "rust", ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".cs": "jvm", ".swift": "swift",
    ".rb": "ruby", ".php": "php",
}
_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    "js": [(re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
           (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
           (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"), "function"),
           (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface"),
           (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*="), "type"),
           (re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)"), "enum")],
    "go": [(re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]"), "func"),
           (re.compile(r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)"), "type")],
    "rust": [(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"), "fn"),
             (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|union)\s+([A-Za-z_]\w*)"), "type"),
             (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>,\s]+\s+for\s+)?([A-Za-z_]\w*)"), "impl")],
    "jvm": [(re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|final|open|sealed|data|static|partial)\s+)*"
                        r"(?:class|interface|object|enum|record|struct)\s+([A-Za-z_]\w*)"), "class"),
            (re.compile(r"^\s*(?:(?:public|private|protected|internal|override|open|static|suspend|final|abstract)\s+)+"
                        r"(?:fun\s+(?:<[^>]*>\s*)?([A-Za-z_]\w*)|[\w<>\[\],.?\s]+?\s+([A-Za-z_]\w*)\s*\()"), "method")],
    "swift": [(re.compile(r"^\s*(?:(?:public|private|internal|open|final)\s+)*(?:class|struct|enum|protocol|actor|extension)\s+([A-Za-z_]\w*)"), "type"),
              (re.compile(r"^\s*(?:(?:public|private|internal|open|static|override)\s+)*func\s+([A-Za-z_]\w*)"), "func")],
    "ruby": [(re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)"), "class"), (re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)"), "def")],
    "php": [(re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)"), "class"),
            (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)"), "function")],
}
_cache: "OrderedDict[str, tuple[tuple, list]]" = OrderedDict()
_CACHE_SIZE = 8


def _py_symbols(source: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[tuple[int, str]] = []

    def sig(node) -> str:
        a = node.args
        names = [x.arg for x in [*a.posonlyargs, *a.args]]
        if a.vararg:
            names.append("*" + a.vararg.arg)
        names += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            names.append("**" + a.kwarg.arg)
        return f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({', '.join(names)})"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((0, sig(node)))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            out.append((0, f"class {node.name}" + (f"({bases})" if bases else "")))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("__"):
                    out.append((1, sig(child)))
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper():
            out.append((0, node.targets[0].id))
    return out


def _regex_symbols(source: str, lang: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    patterns = _PATTERNS[lang]
    for line in source.split("\n")[:6000]:
        if len(line) > 300 or not line.strip():
            continue
        for rx, kind in patterns:
            m = rx.match(line)
            if m:
                name = next((g for g in reversed(m.groups()) if g), "")
                if name and name not in ("if", "for", "while", "switch", "return", "catch"):
                    indent = 1 if line[:1] in (" ", "\t") else 0
                    out.append((indent, f"{kind} {name}"))
                break
    return out


def symbols_for(path: Path, source: str) -> list[tuple[int, str]]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _py_symbols(source)
    lang = _C_LIKE.get(suffix)
    return _regex_symbols(source, lang) if lang else []


def _names(symbols) -> list[str]:
    out = []
    for _, text in symbols:
        m = re.search(r"(?:class|def|func|fn|type|function|interface|enum|method|impl)\s+([A-Za-z_$][\w$]*)", text)
        out.append(m.group(1) if m else text.split("(")[0].strip())
    return [n for n in out if len(n) >= 4]


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _git_listing(root: Path) -> list[str] | None:
    """Tracked + untracked-but-not-ignored files under root (relative, posix), or None when root is not in a git repo."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                              capture_output=True, timeout=20, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk_listing(root: Path, deadline: float) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            out.append(name if rel_dir == "." else f"{rel_dir}/{name}")
        if time.monotonic() > deadline or len(out) > MAX_FILES * 10:
            break
    return out


def list_source_files(root: Path, deadline: float | None = None) -> list[str]:
    """Source files under root worth mapping, relative and sorted. Inside a git checkout this honours .gitignore (so
    build output such as target/ or dist/ is never walked); elsewhere it walks, skipping vendored/hidden folders."""
    root = Path(root)
    deadline = deadline if deadline is not None else time.monotonic() + TIME_BUDGET_S
    listed = _git_listing(root)
    if listed is None:
        listed = _walk_listing(root, deadline)
    wanted = {".py", *_C_LIKE}
    out = []
    for rel in listed:
        parts = rel.split("/")
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        if os.path.splitext(parts[-1])[1].lower() in wanted:
            out.append(rel)
    out.sort()
    return out


def build(root: Path, max_tokens: int = DEFAULT_TOKENS) -> str:
    root = Path(root)
    deadline = time.monotonic() + TIME_BUDGET_S
    candidates = list_source_files(root, deadline)
    truncated = len(candidates) > MAX_FILES
    files: list[tuple[str, str, list]] = []
    for rel in candidates[:MAX_FILES]:
        if time.monotonic() > deadline:
            truncated = True
            break
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        syms = symbols_for(path, source)
        if syms:
            files.append((rel, source, syms))
    if not files:
        return "No source files with recognisable declarations were found."

    # importance: how many other files mention a symbol this file defines. One pass tokenises every file into a set of
    # identifiers and counts, per identifier, the files containing it - linear in the code size, not symbols x files.
    idents = {rel: set(_IDENT.findall(src)) for rel, src, _ in files}
    mentions: Counter = Counter()
    for found in idents.values():
        mentions.update(found)
    scored = []
    for rel, _, syms in files:
        own = idents[rel]
        score = sum(mentions[nm] - (1 if nm in own else 0) for nm in set(_names(syms)))
        scored.append((score, rel, syms))
    scored.sort(key=lambda t: (-t[0], t[1]))

    budget = int(max_tokens * CHARS_PER_TOKEN)
    lines, used, shown = [], 0, 0
    for _score, rel, syms in scored:
        block = [rel] + [("  " * (1 + depth)) + text for depth, text in syms[:40]]
        if len(syms) > 40:
            block.append(f"  ... ({len(syms) - 40} more)")
        size = sum(len(b) + 1 for b in block)
        if used + size > budget and shown:
            break
        lines.extend(block)
        used += size
        shown += 1
    header = f"Repository map: {shown} of {len(files)} files with declarations, most-referenced first."
    if shown < len(files):
        header += f" ({len(files) - shown} more not shown: raise max_tokens or pass a sub-folder as path.)"
    if truncated:
        header += (f" (Truncated: the folder has more source files than the scan budget of {MAX_FILES} files / "
                   f"{TIME_BUDGET_S:.0f}s; pass a sub-folder as path for the rest.)")
    return header + "\n\n" + "\n".join(lines)


def _signature(root: Path) -> tuple:
    sig = []
    for rel in list_source_files(root)[:MAX_FILES + 1]:
        try:
            st = os.stat(os.path.join(root, rel))
            sig.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


def cached_build(root: Path, max_tokens: int) -> str:
    key = f"{Path(root).resolve()}|{max_tokens}"
    sig = _signature(root)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        _cache.move_to_end(key)
        return hit[1]
    text = build(root, max_tokens)
    _cache[key] = (sig, text)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return text


async def _repo_map(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    workspace = Path(workspace).resolve()
    root = safe_path(workspace, inp.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"{inp.get('path') or '.'!r} is not a folder")
    try:
        tokens = max(300, min(int(inp.get("max_tokens") or DEFAULT_TOKENS), MAX_TOKENS))
    except (TypeError, ValueError) as exc:
        raise ToolError("max_tokens must be a number") from exc
    return await asyncio.to_thread(cached_build, root, tokens)


def register_all() -> None:
    toolspec.register(
        {"name": "repo_map",
         "description": "An outline of the codebase: files with the classes and functions they define, most-referenced "
                        "files first, within a token budget. Use it to orient yourself in an unfamiliar project before "
                        "reading files. Pass path to map one folder; max_tokens to see more.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "max_tokens": {"type": "integer"}},
                          "required": []}},
        toolspec.ToolSpec("repo_map", "read", read_only=True, concurrency_safe=True, max_output_chars=60_000,
                          origin="registered"), _repo_map)


register_all()
