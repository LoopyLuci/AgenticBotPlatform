"""TokenJuice: tool output compacted before it enters a model's context, without losing anything.

    1 size gate     outputs under min_bytes (2 KB) pass through
    2 detect        json, diff, html, search (path:line:text hits), code, log, text: a per-tool prior first (grep ->
                    search, git_diff -> diff, run_shell -> log), then cheap structural checks
    3 compress      one compressor per kind; if it declines or does not shrink the text, the original passes through
                    (it never grows)
    4 keep          a lossy result of at least keep_min_chars stores the original under a handle; a footer
                    "⟦tj:<handle>⟧" tells the model, and the tool_output tool reads it back (lines, or a pattern)
    5 account       characters and tokens saved, per tool and kind (stats())

Compressors:  json  arrays of objects as a table; long arrays keep head, tail, error rows and outliers
              diff  changed lines and hunk headers; long unchanged runs collapsed; lockfiles to a one-line summary
              html  readable text          search  hits grouped by file, the densest kept, "[+N more]"
              code  imports, signatures and marked lines (TODO/FIXME/error/panic/unsafe); bodies collapsed
              log   errors, warnings, stack traces and summary lines, with head and tail; repeats collapsed
              text  head and tail
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Callable, Optional

MIN_BYTES = 2048
KEEP_MIN_CHARS = 2000
_PRIOR = {"grep": "search", "search_files": "search", "ripgrep": "search", "git_diff": "diff", "run_shell": "log",
          "run_tests": "log", "web_fetch": "html", "read_file": "", "list_dir": ""}
_lock = threading.Lock()
_stats: dict = {"calls": 0, "compressed": 0, "chars_in": 0, "chars_out": 0, "by_kind": {}}
_store: "OrderedDict[str, str]" = OrderedDict()
_STORE_MAX = 200


def enabled() -> bool:
    return os.environ.get("ABP_TOKENJUICE", "1").lower() not in ("0", "off", "false", "no")


# ---- detection ----------------------------------------------------------------------------------------------------- #

_SEARCH_LINE = re.compile(r"^[^\s:][^:\n]{0,300}:\d+:")
_DIFF = re.compile(r"^(diff --git |@@ -\d+(,\d+)? \+\d+(,\d+)? @@|--- a/|\+\+\+ b/)", re.M)
_CODE = re.compile(r"^\s*(def |class |fn |pub fn |func |function |import |from \S+ import |#include |package |public class )", re.M)
_LOGISH = re.compile(r"\b(error|warning|warn|info|debug|fail(ed|ure)?|traceback|exception|panic)\b", re.I)


def detect(text: str, tool: str = "") -> str:
    prior = _PRIOR.get(tool)
    if prior:
        if prior == "search" and not any(_SEARCH_LINE.match(x) for x in text.splitlines()[:20]):
            prior = ""
        elif prior == "diff" and not _DIFF.search(text[:5000]):
            prior = ""
        if prior:
            return prior
    s = text.lstrip()
    if s[:1] in "[{":
        try:
            json.loads(s)
            return "json"
        except ValueError:
            pass
    if _DIFF.search(text[:5000]):
        return "diff"
    if re.search(r"<(html|body|div|p|span|table)\b", text[:5000], re.I):
        return "html"
    lines = text.splitlines()
    if lines and sum(1 for x in lines[:200] if _SEARCH_LINE.match(x)) >= max(3, min(len(lines), 200) * 0.6):
        return "search"
    if len(_CODE.findall(text[:20000])) >= 3:
        return "code"
    if len(_LOGISH.findall(text[:20000])) >= 3 or tool == "run_shell":
        return "log"
    return "text"


# ---- compressors: (text) -> (compressed, lossy) or None to decline -------------------------------------------------- #

def _json(text: str) -> Optional[tuple[str, bool]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    lossy = False

    def shrink(v, depth=0):
        nonlocal lossy
        if isinstance(v, list) and len(v) > 40:
            def keyed(x):
                return isinstance(x, dict) and any(re.search(r"err|fail|warn", str(k) + str(x.get(k, "")), re.I) for k in x)
            keep = set(range(10)) | set(range(len(v) - 5, len(v))) | {i for i, x in enumerate(v) if keyed(x)}
            nums = [x for x in v if isinstance(x, (int, float))]
            if nums:
                mean = sum(nums) / len(nums)
                sd = (sum((x - mean) ** 2 for x in nums) / len(nums)) ** 0.5 or 1
                keep |= {i for i, x in enumerate(v) if isinstance(x, (int, float)) and abs(x - mean) > 3 * sd}
            lossy = True
            out = [shrink(v[i], depth + 1) for i in sorted(keep)]
            out.insert(min(10, len(out)), f"... {len(v) - len(keep)} more items ...")
            return out
        if isinstance(v, list):
            return [shrink(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: shrink(x, depth + 1) for k, x in v.items()}
        if isinstance(v, str) and len(v) > 500:
            lossy = True
            return v[:400] + f"... ({len(v) - 400} more characters)"
        return v
    data = shrink(data)
    rows = data if isinstance(data, list) else None
    if rows and all(isinstance(r, dict) for r in rows if not isinstance(r, str)) and len(rows) >= 5:
        cols = list(dict.fromkeys(k for r in rows if isinstance(r, dict) for k in r))[:12]
        if cols:
            lines = [" | ".join(cols)]
            for r in rows:
                lines.append(r if isinstance(r, str) else " | ".join(_cell(r.get(c)) for c in cols))
            return "\n".join(lines), lossy or any(len(r) > 12 for r in rows if isinstance(r, dict))
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False), lossy


def _cell(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= 60 else s[:57] + "..."


_LOCK_FILES = re.compile(r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|uv\.lock|Gemfile\.lock|go\.sum)$")


def _diff(text: str) -> Optional[tuple[str, bool]]:
    out, lossy = [], False
    files = re.split(r"(?m)^(?=diff --git )", text)
    for f in files:
        if not f.strip():
            continue
        head = f.splitlines()[0] if f.splitlines() else ""
        if _LOCK_FILES.search(head):
            plus = sum(1 for x in f.splitlines() if x.startswith("+") and not x.startswith("+++"))
            minus = sum(1 for x in f.splitlines() if x.startswith("-") and not x.startswith("---"))
            out.append(f"{head}\n  (lockfile: +{plus}/-{minus} lines)")
            lossy = True
            continue
        run: list[str] = []
        for line in f.splitlines():
            if line.startswith(" ") and not line.startswith(("+++", "---")):
                run.append(line)
                continue
            if len(run) > 6:
                out += run[:2] + [f"   ... {len(run) - 4} unchanged lines ..."] + run[-2:]
                lossy = True
            else:
                out += run
            run = []
            out.append(line)
        out += run[:2] + ([f"   ... {len(run) - 2} unchanged lines ..."] if len(run) > 2 else [])
        lossy = lossy or len(run) > 2
    return "\n".join(out), lossy


def _html(text: str) -> Optional[tuple[str, bool]]:
    from html import unescape
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", text)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", "\n", t)
    t = unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip(), False


def _search(text: str, query: str = "") -> Optional[tuple[str, bool]]:
    by: "OrderedDict[str, list[str]]" = OrderedDict()
    other = []
    for line in text.splitlines():
        m = re.match(r"^([^:\n]{1,300}):(\d+):(.*)$", line)
        if m:
            by.setdefault(m.group(1), []).append(f"{m.group(2)}: {m.group(3).strip()[:200]}")
        elif line.strip():
            other.append(line)
    if not by:
        return None
    terms = [w.lower() for w in re.findall(r"\w{3,}", query)]
    out, lossy = [], False
    for path, hits in by.items():
        if terms:
            hits = sorted(hits, key=lambda h: -sum(h.lower().count(t) for t in terms))
        shown = hits[:8]
        out.append(f"{path}:")
        out += [f"  {h}" for h in shown]
        if len(hits) > len(shown):
            out.append(f"  [+{len(hits) - len(shown)} more]")
            lossy = True
    return "\n".join(out + other[:10]), lossy or len(other) > 10


_MARKED = re.compile(r"\b(TODO|FIXME|XXX|HACK|error|panic|unsafe|raise|throw)\b")


def _code(text: str) -> Optional[tuple[str, bool]]:
    out, body, lossy = [], 0, False
    for line in text.splitlines():
        s = line.strip()
        signature = bool(_CODE.match(line)) or s.startswith(("@", "#!", "export ", "interface ", "type ", "struct ", "enum ", "impl "))
        top = not line[:1].isspace()
        if signature or top or _MARKED.search(line):
            if body:
                out.append(f"    {{ ... {body} lines ... }}")
                lossy = True
                body = 0
            out.append(line)
        else:
            body += 1
    if body:
        out.append(f"    {{ ... {body} lines ... }}")
        lossy = True
    return "\n".join(out), lossy


_KEEP_LOG = re.compile(r"\b(error|fail(ed|ure)?|warn(ing)?|exception|traceback|panic|fatal|denied|refused|not found|"
                       r"passed|failed|summary|total|finished|exit (code|status))\b|^\s+at |^\s+File \"|^E\s", re.I)


def _log(text: str) -> Optional[tuple[str, bool]]:
    lines = text.splitlines()
    if len(lines) < 40:
        return None
    keep = set(range(15)) | set(range(len(lines) - 25, len(lines)))
    for i, x in enumerate(lines):
        if _KEEP_LOG.search(x):
            keep |= {i - 1, i, i + 1}
    out, prev_skip, seen = [], False, Counter()
    for i, x in enumerate(lines):
        if i not in keep:
            if not prev_skip:
                out.append("...")
            prev_skip = True
            continue
        norm = re.sub(r"\d+", "#", x)
        seen[norm] += 1
        if seen[norm] == 4:
            out.append("   (lines like this repeat; further copies omitted)")
        if seen[norm] > 3:
            continue
        out.append(x)
        prev_skip = False
    return "\n".join(out), True


def _text(text: str) -> Optional[tuple[str, bool]]:
    if len(text) < 6000:
        return None
    return text[:3000] + f"\n... [{len(text) - 4500} characters in between] ...\n" + text[-1500:], True


COMPRESSORS: dict[str, Callable[..., Optional[tuple[str, bool]]]] = {
    "json": _json, "diff": _diff, "html": _html, "search": _search, "code": _code, "log": _log, "text": _text}


# ---- the router ---------------------------------------------------------------------------------------------------- #

def _keep(text: str) -> str:
    handle = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    with _lock:
        _store[handle] = text
        _store.move_to_end(handle)
        while len(_store) > _STORE_MAX:
            _store.popitem(last=False)
    d = _spill_dir()
    if d is not None:
        try:
            (d / f"{handle}.txt").write_text(text, encoding="utf-8", errors="replace")
        except OSError:
            pass
    return handle


def _spill_dir() -> Optional[Path]:
    base = os.environ.get("ABP_AGENT_STATE_DIR", "").strip()
    if not base:
        from bot.envfile import PROJECT_ROOT
        base = str(PROJECT_ROOT / "data" / "agent-state")
    p = Path(base) / "tokenjuice"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return None


def compress(text: str, tool: str = "", query: str = "") -> str:
    """The compacted form of a tool's output (the text itself when compacting would not help)."""
    if not enabled() or not isinstance(text, str) or len(text.encode("utf-8", "replace")) < MIN_BYTES:
        return text
    kind = detect(text, tool)
    fn = COMPRESSORS[kind]
    try:
        res = fn(text, query) if kind == "search" else fn(text)
    except Exception:  # noqa: BLE001 - a compressor failing never loses the output
        res = None
    if res is None and kind not in ("text",):
        res = _text(text)
        kind = "text" if res else kind
    if res is None or len(res[0]) >= len(text) * 0.9:
        _account(kind, len(text), len(text), False)
        return text
    out, lossy = res
    if lossy and len(text) >= KEEP_MIN_CHARS:
        handle = _keep(text)
        out += (f"\n\n⟦tj:{handle}⟧ compacted {kind} output ({len(text):,} → {len(out):,} characters). The full original is "
                f"kept: tool_output(handle=\"{handle}\", start=, lines=, pattern=) reads any part of it.")
    _account(kind, len(text), len(out), True)
    return out


def _account(kind: str, n_in: int, n_out: int, compressed: bool) -> None:
    with _lock:
        _stats["calls"] += 1
        _stats["compressed"] += compressed
        _stats["chars_in"] += n_in
        _stats["chars_out"] += n_out
        k = _stats["by_kind"].setdefault(kind, {"calls": 0, "chars_saved": 0})
        k["calls"] += 1
        k["chars_saved"] += n_in - n_out


def stats() -> dict:
    with _lock:
        s = json.loads(json.dumps(_stats))
    s["chars_saved"] = s["chars_in"] - s["chars_out"]
    s["tokens_saved_est"] = s["chars_saved"] // 4
    return s


def read(handle: str, start: int = 1, lines: int = 200, pattern: str = "") -> str:
    """A kept original: `lines` lines from line `start` (1-based), or the lines matching `pattern` with line numbers."""
    with _lock:
        text = _store.get(handle)
    if text is None:
        d = _spill_dir()
        f = d / f"{re.sub(r'[^0-9a-f]', '', handle)}.txt" if d else None
        if f is None or not f.is_file():
            raise KeyError(f"no kept output {handle!r} (kept outputs last for this session's recent calls)")
        text = f.read_text(encoding="utf-8", errors="replace")
    rows = text.splitlines()
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            raise ValueError(f"pattern: {e}") from e
        hits = [f"{i}: {x}" for i, x in enumerate(rows, 1) if rx.search(x)]
        return "\n".join(hits[:300]) + (f"\n[+{len(hits) - 300} more matches]" if len(hits) > 300 else "") if hits else "no line matches"
    start = max(1, int(start))
    part = rows[start - 1: start - 1 + max(1, min(int(lines), 2000))]
    more = len(rows) - (start - 1 + len(part))
    return "\n".join(part) + (f"\n[{more} more lines; total {len(rows)}]" if more > 0 else f"\n[end; total {len(rows)} lines]")
