"""Editing, search and planning tools for the native agent (roadmap P1).

    edit_file    replace text in a file (exact match, with a careful fuzzy fallback)
    multi_edit   several edits to one file, all or nothing
    apply_patch  a unified diff across one or more files, all or nothing
    grep         search file contents (regular expressions)
    glob         find files by pattern
    todo_write / todo_read   the agent's own task list for the session

Every path goes through the workspace guard. Changing an existing file requires that
the agent has read it in this session and that it has not changed since: an edit is
then always based on what is really there, and a concurrent change (a person, a
formatter, another agent) is noticed instead of overwritten. Set
`native_agent.require_read_before_write: false` to relax that.

Nothing here needs a third-party package.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError, safe_path
from bot.agent_runtime.state import safe_name, state_dir

SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
                       ".pytest_cache", ".tox", "target", "dist", "build", toolspec.SPILL_DIR})
MAX_FILE_BYTES = 2_000_000
GREP_DEFAULT_RESULTS = 200
GLOB_MAX = 200
SEARCH_TIME_BUDGET_S = 20.0
DIFF_MAX_LINES = 60
NOTEBOOK_OUTPUT_CHARS = 500
PDF_MAX_PAGES = 50


# ---- tracking what the agent has read -----------------------------------------
_reads: dict[tuple[str, str], tuple[int, int]] = {}


def _sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def record_read(path: Path) -> None:
    try:
        _reads[(toolspec.current_session(), str(path))] = _sig(path)
    except OSError:
        pass


def _require_read_first() -> bool:
    try:
        from bot.config import config

        return bool((config.current.get("native_agent") or {}).get("require_read_before_write", True))
    except Exception:  # noqa: BLE001
        return True


def require_fresh(path: Path, rel: str) -> None:
    """Refuse to change a file the agent has not read, or that changed after it did."""
    if not _require_read_first():
        return
    seen = _reads.get((toolspec.current_session(), str(path)))
    if seen is None:
        raise ToolError(f"read {rel} with read_file before changing it, so the change is based on what is really there")
    try:
        now = _sig(path)
    except OSError:
        raise ToolError(f"{rel} is no longer readable")
    if now != seen:
        raise ToolError(f"{rel} changed on disk after you read it; read it again before changing it")


def forget_reads() -> None:
    _reads.clear()


# ---- reading and writing text ---------------------------------------------------
def _load(path: Path, rel: str) -> tuple[str, str]:
    """(text with LF line ends, the file's original line ending)."""
    if path.stat().st_size > MAX_FILE_BYTES * 5:
        raise ToolError(f"{rel} is too large to edit ({path.stat().st_size} bytes)")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{rel} is a binary file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{rel} is not valid UTF-8 text")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol


def _save(path: Path, text: str, eol: str) -> None:
    """Atomic: write beside the target, then replace, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".abp-tmp")
    data = (text.replace("\n", eol) if eol != "\n" else text).encode("utf-8")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _diff(before: str, after: str, rel: str) -> str:
    lines = list(difflib.unified_diff(before.split("\n"), after.split("\n"), f"a/{rel}", f"b/{rel}", n=2, lineterm=""))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... ({len(lines) - DIFF_MAX_LINES} more diff lines)"]
    return "\n".join(lines)


# ---- edits ----------------------------------------------------------------------
def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> tuple[str, int, str]:
    """Replace `old` with `new` in `text`. Returns (new text, replacements, how).
    Exact match first. If there is none, a whole-line match that ignores indentation
    and trailing whitespace is accepted when it is unique, with the replacement
    re-indented to fit. Anything ambiguous is refused, never guessed."""
    if old == new:
        raise ToolError("old_string and new_string are identical; nothing to change")
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return text.replace(old, new), count, "exact"
    if count > 1:
        where = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)][:5]
        raise ToolError(f"old_string matches {count} places (lines {where}); add surrounding lines so it is unique, "
                        "or set replace_all to change every one")

    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines.pop()
    new_lines = new.split("\n")
    if old.endswith("\n") and new_lines and new_lines[-1] == "":
        new_lines.pop()
    if not any(line.strip() for line in old_lines):
        raise ToolError("old_string is blank")
    lines = text.split("\n")
    width = len(old_lines)
    hits = [i for i in range(len(lines) - width + 1)
            if all(lines[i + k].strip() == old_lines[k].strip() for k in range(width))]
    if not hits:
        first = next(line.strip() for line in old_lines if line.strip())
        close = difflib.get_close_matches(first, [ln.strip() for ln in lines if ln.strip()], n=1, cutoff=0.6)
        hint = ""
        if close:
            at = next(i for i, ln in enumerate(lines) if ln.strip() == close[0]) + 1
            hint = f" The closest line is {at}: {close[0][:120]!r}."
        raise ToolError("old_string was not found in the file." + hint + " Read the file again and copy the text exactly.")
    if len(hits) > 1:
        raise ToolError(f"old_string matches {len(hits)} places once whitespace is ignored "
                        f"(lines {[h + 1 for h in hits[:5]]}); add surrounding lines so it is unique")
    at = hits[0]
    file_indent = _indent(next(lines[at + k] for k in range(width) if lines[at + k].strip()))
    old_indent = _indent(next(ln for ln in old_lines if ln.strip()))
    if file_indent != old_indent:
        new_lines = [file_indent + ln[len(old_indent):] if ln.startswith(old_indent) else ln for ln in new_lines]
    lines[at:at + width] = new_lines
    return "\n".join(lines), 1, "whitespace-insensitive"


async def _edit_file(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    rel = inp.get("path") or ""
    path = safe_path(workspace, rel)
    old, new = inp.get("old_string"), inp.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("old_string and new_string are required")
    if not path.exists():
        if old == "":
            _save(path, new, "\n")
            record_read(path)
            return f"Created {rel} ({len(new)} characters)."
        raise ToolError(f"{rel!r} does not exist (to create a file use write_file, or edit_file with an empty old_string)")
    if not path.is_file():
        raise ToolError(f"{rel!r} is not a file")
    if old == "":
        raise ToolError("old_string is empty; to replace a whole file use write_file")
    require_fresh(path, rel)
    text, eol = _load(path, rel)
    new_text, n, how = apply_edit(text, old, new, bool(inp.get("replace_all")))
    _save(path, new_text, eol)
    record_read(path)
    note = "" if how == "exact" else f" ({how} match)"
    return f"Edited {rel}: {n} replacement{'s' if n != 1 else ''}{note}.\n{_diff(text, new_text, rel)}"


async def _multi_edit(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    rel = inp.get("path") or ""
    path = safe_path(workspace, rel)
    edits = inp.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits must be a non-empty list of {old_string, new_string}")
    if len(edits) > 50:
        raise ToolError("at most 50 edits per call")
    if not path.is_file():
        raise ToolError(f"{rel!r} does not exist or is not a file")
    require_fresh(path, rel)
    original, eol = _load(path, rel)
    text, total = original, 0
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("old_string"), str) or not isinstance(edit.get("new_string"), str):
            raise ToolError(f"edit {i}: needs old_string and new_string")
        if edit["old_string"] == "":
            raise ToolError(f"edit {i}: old_string is empty")
        try:
            text, n, _ = apply_edit(text, edit["old_string"], edit["new_string"], bool(edit.get("replace_all")))
        except ToolError as exc:
            raise ToolError(f"edit {i} of {len(edits)}: {exc} (nothing was changed)")
        total += n
    _save(path, text, eol)
    record_read(path)
    return f"Edited {rel}: {len(edits)} edits, {total} replacements.\n{_diff(original, text, rel)}"


# ---- unified diffs --------------------------------------------------------------
@dataclass
class _Hunk:
    old_start: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class _FilePatch:
    old_name: Optional[str]
    new_name: Optional[str]
    hunks: list[_Hunk] = field(default_factory=list)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_name(raw: str) -> Optional[str]:
    name = raw.split("\t")[0].strip()
    if name == "/dev/null":
        return None
    if name.startswith(("a/", "b/")):
        name = name[2:]
    return name.strip('"')


def parse_patch(patch: str) -> list[_FilePatch]:
    lines = patch.replace("\r\n", "\n").split("\n")
    i, files = 0, []
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old_name = _patch_name(lines[i][4:])
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ToolError("malformed patch: a '--- ' line must be followed by a '+++ ' line")
        fp = _FilePatch(old_name, _patch_name(lines[i][4:]))
        i += 1
        while i < len(lines) and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if not m:
                raise ToolError(f"malformed hunk header: {lines[i][:80]!r}")
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunk = _Hunk(old_start=int(m.group(1)))
            i += 1
            while (len(hunk.old) < old_count or len(hunk.new) < new_count) and i < len(lines):
                line = lines[i]
                tag, body = (line[:1], line[1:])
                if line.startswith("\\"):
                    i += 1
                    continue
                if tag == "+":
                    hunk.new.append(body)
                elif tag == "-":
                    hunk.old.append(body)
                elif tag == " " or line == "":
                    hunk.old.append(body)
                    hunk.new.append(body)
                else:
                    break
                i += 1
            if len(hunk.old) != old_count or len(hunk.new) != new_count:
                raise ToolError(f"hunk at line {hunk.old_start} is shorter than its header says")
            fp.hunks.append(hunk)
        if not fp.hunks:
            raise ToolError(f"patch for {fp.new_name or fp.old_name} has no hunks")
        files.append(fp)
    if not files:
        raise ToolError("no file changes found; expected a unified diff ('--- a/x', '+++ b/x', '@@ ... @@')")
    return files


def _find(cur: list[str], old: list[str], guess: int) -> Optional[int]:
    """Where `old` occurs in `cur`, nearest to `guess`; exact first, then ignoring trailing whitespace."""
    if not old:
        return max(0, min(guess, len(cur)))
    for norm in (lambda s: s, lambda s: s.rstrip()):
        want = [norm(x) for x in old]
        best = None
        for p in range(0, len(cur) - len(old) + 1):
            if [norm(x) for x in cur[p:p + len(old)]] == want and (best is None or abs(p - guess) < abs(best - guess)):
                best = p
        if best is not None:
            return best
    return None


def _apply_hunks(text: str, fp: _FilePatch, label: str) -> str:
    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    offset = 0
    for n, hunk in enumerate(fp.hunks, 1):
        guess = max(hunk.old_start - 1, 0) + offset
        at = _find(lines, hunk.old, guess)
        if at is None:
            raise ToolError(f"hunk {n} for {label} does not apply: its context was not found near line {hunk.old_start}")
        lines[at:at + len(hunk.old)] = hunk.new
        offset += len(hunk.new) - len(hunk.old)
    return "\n".join(lines) + ("\n" if trailing or not text else "")


async def _apply_patch(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    patch = inp.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        raise ToolError("patch is required (a unified diff)")
    plan: list[tuple[Path, Optional[str], str, str]] = []      # path, new text (None = delete), eol, label
    removals: list[Path] = []
    for fp in parse_patch(patch):
        target_rel = fp.new_name or fp.old_name
        if fp.old_name is None:                               # a new file
            path = safe_path(workspace, fp.new_name)
            if path.exists():
                raise ToolError(f"{fp.new_name} already exists; a patch that creates a file cannot overwrite it")
            plan.append((path, _apply_hunks("", fp, fp.new_name), "\n", fp.new_name))
            continue
        src = safe_path(workspace, fp.old_name)
        if not src.is_file():
            raise ToolError(f"{fp.old_name} does not exist")
        require_fresh(src, fp.old_name)
        text, eol = _load(src, fp.old_name)
        new_text = _apply_hunks(text, fp, fp.old_name)
        if fp.new_name is None:                               # deleted
            if new_text.strip():
                raise ToolError(f"patch deletes {fp.old_name} but leaves content behind")
            removals.append(src)
        elif fp.new_name != fp.old_name:                      # renamed
            dest = safe_path(workspace, fp.new_name)
            if dest.exists():
                raise ToolError(f"{fp.new_name} already exists")
            plan.append((dest, new_text, eol, fp.new_name))
            removals.append(src)
        else:
            plan.append((src, new_text, eol, target_rel))
    for path, new_text, eol, _ in plan:                       # nothing is written until every hunk applied
        _save(path, new_text, eol)
        record_read(path)
    for path in removals:
        path.unlink()
        _reads.pop((toolspec.current_session(), str(path)), None)
    names = [label for _, _, _, label in plan] + [f"(deleted) {p.name}" for p in removals]
    return f"Patch applied to {len(names)} file(s): " + ", ".join(names)


# ---- search ---------------------------------------------------------------------
def _walk(root: Path, deadline: float):
    """Files under root, skipping vendored and generated folders (unless root is one)."""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if time.monotonic() > deadline:
                return
            yield Path(dirpath) / name


def _matches_glob(rel: str, pattern: Optional[str]) -> bool:
    if not pattern:
        return True
    target = rel if "/" in pattern else rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(target, pattern)


def _grep_sync(workspace: Path, inp: dict) -> str:
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern is required")
    workspace = Path(workspace).resolve()
    root = safe_path(workspace, inp.get("path") or ".")
    if not root.exists():
        raise ToolError(f"{inp.get('path')!r} does not exist")
    flags = re.IGNORECASE if inp.get("ignore_case") else 0
    try:
        rx = re.compile(re.escape(pattern) if inp.get("fixed_strings") else pattern, flags)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}")
    mode = inp.get("output_mode") or "content"
    if mode not in ("content", "files", "count"):
        raise ToolError("output_mode must be content, files or count")
    context = max(0, min(int(inp.get("context") or 0), 10))
    limit = max(1, min(int(inp.get("max_results") or GREP_DEFAULT_RESULTS), 2000))
    deadline = time.monotonic() + SEARCH_TIME_BUDGET_S
    out: list[str] = []
    files_with, counts, total, truncated = [], {}, 0, False
    for file in _walk(root, deadline):
        rel = file.relative_to(workspace).as_posix()
        if not _matches_glob(rel, inp.get("glob")):
            continue
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        lines = raw.decode("utf-8", errors="replace").split("\n")
        hits = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hits:
            continue
        files_with.append(rel)
        counts[rel] = len(hits)
        if mode != "content":
            continue
        shown = -1
        for h in hits:
            if total >= limit:
                truncated = True
                break
            lo, hi = max(0, h - context), min(len(lines) - 1, h + context)
            if context and shown >= 0 and lo > shown + 1:
                out.append("--")
            for i in range(max(lo, shown + 1), hi + 1):
                sep = ":" if i == h else "-"
                out.append(f"{rel}{sep}{i + 1}{sep}{lines[i][:300]}")
            shown = max(shown, hi)
            total += 1
        if truncated:
            break
    timed_out = time.monotonic() > deadline
    if mode == "files":
        result = "\n".join(files_with[:limit]) or "No matches."
        return result + (f"\n... (more than {limit} files)" if len(files_with) > limit else "")
    if mode == "count":
        return "\n".join(f"{f}:{c}" for f, c in counts.items()) or "No matches."
    if not out:
        return "No matches." + (" (the search ran out of time)" if timed_out else "")
    note = ""
    if truncated:
        note = f"\n... (stopped at {limit} matches; narrow the pattern, path or glob)"
    elif timed_out:
        note = "\n... (the search ran out of time; narrow the path)"
    return "\n".join(out) + note


async def _grep(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    return await asyncio.to_thread(_grep_sync, workspace, inp)


def _glob_sync(workspace: Path, inp: dict) -> str:
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern is required")
    if Path(pattern).anchor or pattern.startswith(("/", "\\")) or ".." in Path(pattern).parts:
        raise ToolError("pattern must be relative and stay inside the working directory")
    workspace = Path(workspace).resolve()
    root = safe_path(workspace, inp.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"{inp.get('path') or '.'!r} is not a folder")
    found = []
    deadline = time.monotonic() + SEARCH_TIME_BUDGET_S
    for p in root.glob(pattern):
        if time.monotonic() > deadline:
            break
        try:
            rel_parts = p.relative_to(root).parts
            if not p.is_file() or any(part in SKIP_DIRS for part in rel_parts[:-1]):
                continue
            found.append((p.stat().st_mtime, p.relative_to(workspace).as_posix()))
        except (OSError, ValueError):
            continue
    if not found:
        return "No files match."
    found.sort(key=lambda t: (-t[0], t[1]))
    shown = [name for _, name in found[:GLOB_MAX]]
    more = f"\n... ({len(found) - GLOB_MAX} more; narrow the pattern)" if len(found) > GLOB_MAX else ""
    return "\n".join(shown) + more


async def _glob(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    return await asyncio.to_thread(_glob_sync, workspace, inp)


# ---- reading (used by read_file in tools.py) ------------------------------------
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"}


def _notebook_text(path: Path) -> str:
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
        cells = nb["cells"]
    except (OSError, ValueError, KeyError):
        raise ToolError("could not read this notebook as JSON")
    out = []
    for i, cell in enumerate(cells):
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        out.append(f"# [cell {i}: {cell.get('cell_type', '?')}]\n{source}")
        for o in cell.get("outputs", []) or []:
            text = o.get("text") or (o.get("data") or {}).get("text/plain") or ""
            text = "".join(text) if isinstance(text, list) else str(text)
            if text.strip():
                out.append(f"# [output]\n{text[:NOTEBOOK_OUTPUT_CHARS]}")
    return "\n\n".join(out)


def _pdf_text(path: Path) -> str:
    try:
        import pypdf
    except ImportError:
        return "[PDF: reading PDFs needs the optional 'pypdf' package (pip install pypdf); it is not installed here]"
    try:
        reader = pypdf.PdfReader(str(path))
        pages = reader.pages[:PDF_MAX_PAGES]
        text = "\n\n".join(f"# [page {i + 1}]\n{(p.extract_text() or '').strip()}" for i, p in enumerate(pages))
        more = f"\n\n... ({len(reader.pages) - PDF_MAX_PAGES} more pages)" if len(reader.pages) > PDF_MAX_PAGES else ""
        return text + more
    except Exception as exc:  # noqa: BLE001 - a bad PDF is a message, not a crash
        return f"[PDF could not be read: {exc}]"


def read_text(path: Path, rel: str, *, offset: Optional[int] = None, limit: Optional[int] = None,
              numbered: bool = False, max_chars: int = 20000) -> str:
    """What read_file returns. Plain text is unchanged from before unless offset, limit or
    line numbers are asked for; notebooks and PDFs are turned into text; images and other
    binary files are reported, not dumped."""
    suffix = path.suffix.lower()
    record_read(path)
    if suffix in IMAGE_SUFFIXES:
        return f"[image file {rel}, {path.stat().st_size} bytes; it cannot be shown as text]"
    if suffix == ".ipynb":
        text = _notebook_text(path)
    elif suffix == ".pdf":
        text = _pdf_text(path)
    else:
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return f"[binary file {rel}, {len(raw)} bytes; it cannot be shown as text]"
        text = raw.decode("utf-8", errors="replace")
    if offset is None and limit is None and not numbered:
        if len(text) > max_chars:
            return (text[:max_chars] + f"\n… truncated ({len(text)} chars total); "
                    "call read_file again with offset and limit to see the rest")
        return text
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or 2000), 5000))
    chunk = lines[start - 1:start - 1 + count]
    body = "\n".join(f"{start + i:>6}\t{line}" for i, line in enumerate(chunk)) if numbered or offset or limit \
        else "\n".join(chunk)
    left = len(lines) - (start - 1 + len(chunk))
    if left > 0:
        body += f"\n… {left} more lines; call again with offset={start + len(chunk)}"
    if not chunk:
        body = f"(no lines at offset {start}; the file has {len(lines)} lines)"
    return body


def check_overwrite(path: Path, rel: str) -> None:
    """write_file over an existing file follows the same read-first rule as edits."""
    if path.exists() and path.is_file():
        require_fresh(path, rel)


# ---- the agent's own todo list --------------------------------------------------
TODO_STATUSES = ("pending", "in_progress", "completed")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _todo_file() -> Path:
    return state_dir("todos") / f"{safe_name(toolspec.current_session())}.json"


def _todo_load() -> list[dict]:
    try:
        return json.loads(_todo_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _todo_render(items: list[dict]) -> str:
    if not items:
        return "The todo list is empty."
    done = sum(1 for t in items if t["status"] == "completed")
    body = "\n".join(f"{_MARK[t['status']]} {t['content']}" for t in items)
    return f"Todo list ({done}/{len(items)} done):\n{body}"


async def _todo_write(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    todos = inp.get("todos")
    if not isinstance(todos, list) or len(todos) > 50:
        raise ToolError("todos must be a list of at most 50 items")
    clean = []
    for i, t in enumerate(todos, 1):
        if not isinstance(t, dict) or not str(t.get("content", "")).strip():
            raise ToolError(f"todo {i}: needs non-empty content")
        status = t.get("status", "pending")
        if status not in TODO_STATUSES:
            raise ToolError(f"todo {i}: status must be one of {', '.join(TODO_STATUSES)}")
        clean.append({"content": str(t["content"]).strip()[:200], "status": status})
    _todo_file().write_text(json.dumps(clean), encoding="utf-8")
    text = _todo_render(clean)
    if sum(1 for t in clean if t["status"] == "in_progress") > 1:
        text += "\n(note: more than one item is in progress; keep to one at a time)"
    return text


async def _todo_read(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    items = _todo_load()
    return _todo_render(items) if items else "No todo list yet."


# ---- registration ---------------------------------------------------------------
def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": required}}


def register_all() -> None:
    S = {"type": "string"}
    toolspec.register(_schema(
        "edit_file",
        "Replace text in an existing file. Give old_string exactly as it appears (with enough surrounding lines to "
        "be unique) and new_string. You must have read the file with read_file first. To create a new file pass an "
        "empty old_string, or use write_file. Set replace_all to change every occurrence. Prefer this to write_file "
        "for changes to existing files.",
        {"path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}},
        ["path", "old_string", "new_string"]), toolspec.ToolSpec("edit_file", "write", origin="registered"), _edit_file)
    toolspec.register(_schema(
        "multi_edit",
        "Make several edits to one file in one call, applied in order; if any edit fails nothing is changed. "
        "Read the file first.",
        {"path": S, "edits": {"type": "array", "items": {"type": "object", "properties": {
            "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}}, "required": ["old_string", "new_string"]}}},
        ["path", "edits"]), toolspec.ToolSpec("multi_edit", "write", origin="registered"), _multi_edit)
    toolspec.register(_schema(
        "apply_patch",
        "Apply a unified diff ('--- a/file', '+++ b/file', '@@ -l,c +l,c @@' hunks) to one or more files, "
        "including creating (--- /dev/null) and deleting (+++ /dev/null) files. All or nothing.",
        {"patch": S}, ["patch"]), toolspec.ToolSpec("apply_patch", "write", origin="registered"), _apply_patch)
    toolspec.register(_schema(
        "grep",
        "Search file contents with a regular expression. output_mode: content (matching lines with line numbers, "
        "default), files (just paths) or count. Narrow with path and glob (e.g. '*.py'). context adds lines "
        "around each match.",
        {"pattern": S, "path": S, "glob": S, "ignore_case": {"type": "boolean"}, "fixed_strings": {"type": "boolean"},
         "context": {"type": "integer"}, "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
         "max_results": {"type": "integer"}}, ["pattern"]),
        toolspec.ToolSpec("grep", "read", read_only=True, concurrency_safe=True, origin="registered"), _grep)
    toolspec.register(_schema(
        "glob",
        "Find files by pattern, e.g. '**/*.py' or 'src/**/test_*.py'. Newest first. Skips .git, node_modules and "
        "similar folders.",
        {"pattern": S, "path": S}, ["pattern"]),
        toolspec.ToolSpec("glob", "read", read_only=True, concurrency_safe=True, origin="registered"), _glob)
    toolspec.register(_schema(
        "todo_write",
        "Keep a task list for a multi-step job: replace the whole list. Each item has content and a status "
        "(pending, in_progress, completed). Use it for work with three or more steps; keep one item in progress; "
        "mark items completed as you finish them.",
        {"todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": S, "status": {"type": "string", "enum": list(TODO_STATUSES)}}, "required": ["content"]}}},
        ["todos"]), toolspec.ToolSpec("todo_write", "agent", needs_approval=False, origin="registered"), _todo_write)
    toolspec.register(_schema("todo_read", "Show your current task list.", {}, []),
                      toolspec.ToolSpec("todo_read", "read", read_only=True, concurrency_safe=True, origin="registered"),
                      _todo_read)


register_all()
