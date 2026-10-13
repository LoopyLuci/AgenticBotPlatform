"""`python -m bot.codeintel bench`: how many tokens the navigation tools return compared with the grep + read_file
route, for questions about ABP's own code. No model is involved; both sides are measured with the same estimate
(characters / 3.5).

The baseline imitates what an agent without these tools does: run `grep` for the identifiers (the grep tool returns
at most 200 `path:line:text` lines) and then `read_file` the files it needs, each returning at most 20,000 characters
(read_file's cap on a whole-file read), up to three files per question. That is a generous baseline - an agent
often reads more - so the ratios are, if anything, conservative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from bot.codeintel import indexer, tools

GREP_MAX_LINES = 200
READ_FILE_CAP_CHARS = 20_000
MAX_FILES_OPENED = 3
CPT = tools.CHARS_PER_TOKEN


@dataclass
class Question:
    text: str
    grep: str                                   # regex the baseline greps for
    open_files: Callable[[Path], list[str]]     # files the baseline then reads (best guesses an agent would make)
    tools: Callable[[Path], list[tools.Result]]
    grep_in: str = ""                           # restrict grep to a folder prefix ("tests/")


def _tok(chars: int) -> int:
    return int(-(-chars // CPT)) if chars else 0


def _grep(root: Path, pattern: str, prefix: str = "") -> tuple[int, list[str]]:
    """(tokens of the grep tool's output, files with hits ordered by hit count)."""
    rx = re.compile(pattern)
    out_chars = 0
    shown = 0
    hits: dict[str, int] = {}
    for rel in indexer.candidate_files(root):
        if prefix and not rel.startswith(prefix):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                hits[rel] = hits.get(rel, 0) + 1
                if shown < GREP_MAX_LINES:
                    out_chars += len(f"{rel}:{n}:{line.strip()[:200]}") + 1
                    shown += 1
    ordered = sorted(hits, key=lambda f: (-hits[f], f))
    return _tok(out_chars), ordered


def _read_tokens(root: Path, rel: str) -> int:
    try:
        return _tok(min((root / rel).stat().st_size, READ_FILE_CAP_CHARS))
    except OSError:
        return 0


QUESTIONS: list[Question] = [
    Question("Where is the repo_map result cached, and when is it invalidated?", r"\bcached_build\b",
             lambda r: ["bot/agent_runtime/repo_map.py"],
             lambda r: [tools.find_symbol(r, "cached_build", limit=3), tools.read_symbol(r, "repo_map.cached_build")]),
    Question("How does safe_path decide a path is outside the workspace?", r"\bsafe_path\b",
             lambda r: ["bot/agent_runtime/errors.py"],
             lambda r: [tools.read_symbol(r, "bot/agent_runtime/errors.py::safe_path")]),
    Question("What calls refresh_code_index?", r"\brefresh_code_index\b", lambda r: [],
             lambda r: [tools.callers(r, "refresh_code_index")]),
    Question("Which tests cover bot/agent_runtime/repo_map.py?", r"\brepo_map\b", lambda r: [],
             lambda r: [tools.related_tests(r, "bot/agent_runtime/repo_map.py")], grep_in="tests/"),
    Question("What is defined in bot/agent_runtime/tools.py?", r"^(?:async )?def |^class ",
             lambda r: ["bot/agent_runtime/tools.py"],
             lambda r: [tools.outline(r, "bot/agent_runtime/tools.py")]),
    Question("How does the agent decide to skip vendored folders when mapping a repository?", r"SKIP_DIRS|vendored",
             lambda r: [], lambda r: [tools.context_pack(r, "how does the agent skip vendored folders when mapping a repository", 3000)]),
    Question("What is affected if I change search_index._match_query?", r"\b_match_query\b", lambda r: [],
             lambda r: [tools.callers(r, "_match_query"), tools.related_tests(r, "_match_query")]),
    Question("Where is the MCP tool get_status defined?", r"\bget_status\b", lambda r: ["bot/mcp_server.py"],
             lambda r: [tools.find_symbol(r, "get_status", limit=3), tools.read_symbol(r, "bot/mcp_server.py::get_status")]),
]


def run(root: Path, questions: Optional[list[Question]] = None) -> dict:
    root = Path(root).resolve()
    tools.ensure(root, chunks=True)
    rows = []
    for q in questions or QUESTIONS:
        grep_tokens, hit_files = _grep(root, q.grep, q.grep_in)
        opened = [f for f in q.open_files(root) if (root / f).is_file()] or hit_files[:MAX_FILES_OPENED]
        base = grep_tokens + sum(_read_tokens(root, f) for f in opened[:MAX_FILES_OPENED])
        try:
            results = q.tools(root)
        except tools.CodeIntelError as exc:
            rows.append({"question": q.text, "skipped": str(exc)})
            continue
        used = sum(r.tokens for r in results)
        rows.append({"question": q.text, "baseline_tokens": base, "tool_tokens": used,
                     "ratio": round(base / used, 1) if used else None,
                     "baseline": f"grep {grep_tokens} + read {len(opened[:MAX_FILES_OPENED])} file(s)",
                     "tools_calls": len(results)})
    done = [r for r in rows if "skipped" not in r and r["tool_tokens"]]
    total_b = sum(r["baseline_tokens"] for r in done)
    total_t = sum(r["tool_tokens"] for r in done)
    return {"rows": rows, "total_baseline": total_b, "total_tools": total_t,
            "total_ratio": round(total_b / total_t, 1) if total_t else None}


def render(report: dict) -> str:
    lines = [f"{'baseline':>9} {'tools':>7} {'ratio':>7}  question"]
    for r in report["rows"]:
        if "skipped" in r:
            lines.append(f"{'-':>9} {'-':>7} {'-':>7}  {r['question']}  (skipped: {r['skipped']})")
        else:
            lines.append(f"{r['baseline_tokens']:>9} {r['tool_tokens']:>7} {str(r['ratio']) + 'x':>7}  {r['question']}")
    lines.append(f"{report['total_baseline']:>9} {report['total_tools']:>7} {str(report['total_ratio']) + 'x':>7}  TOTAL "
                 "(baseline = grep output + read_file of up to 3 files; tools = all tool output for the question)")
    return "\n".join(lines)
