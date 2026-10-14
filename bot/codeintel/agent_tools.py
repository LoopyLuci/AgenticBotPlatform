"""The code-navigation tools, registered for the native agent next to repo_map and code_search (same registry:
bot/agent_runtime/toolspec.py). Each result ends with its token cost. The work runs in a thread, so a large index
build never blocks the event loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError
from bot.codeintel import tools as ci

_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_NAMES = ("outline", "find_symbol", "read_symbol", "references", "callers", "related_tests", "impact", "context_pack")


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(inp.get(key) or default), hi))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key} must be a number") from exc


async def _run(fn, workspace: Path, *args, **kwargs) -> str:
    try:
        result = await asyncio.to_thread(fn, Path(workspace), *args, **kwargs)
    except ci.CodeIntelError as exc:
        raise ToolError(str(exc)) from exc
    return result.render()


async def _outline(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.outline, workspace, inp.get("path") or ".", max_tokens=_int(inp, "max_tokens", 1500, 200, 8000))


async def _find_symbol(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.find_symbol, workspace, inp.get("query") or "", kind=str(inp.get("kind") or ""),
                      limit=_int(inp, "limit", 15, 1, 60), max_tokens=_int(inp, "max_tokens", 900, 200, 6000))


async def _read_symbol(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.read_symbol, workspace, inp.get("target") or inp.get("symbol") or "",
                      context_lines=_int(inp, "context_lines", 0, 0, 40), max_tokens=_int(inp, "max_tokens", 3000, 200, 12000))


async def _references(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.references, workspace, inp.get("symbol") or "", limit=_int(inp, "limit", 60, 1, 300),
                      max_tokens=_int(inp, "max_tokens", 1400, 200, 8000))


async def _callers(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.callers, workspace, inp.get("symbol") or "", limit=_int(inp, "limit", 40, 1, 200),
                      max_tokens=_int(inp, "max_tokens", 1200, 200, 8000))


async def _related_tests(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.related_tests, workspace, inp.get("target") or inp.get("path") or inp.get("symbol") or "",
                      max_tokens=_int(inp, "max_tokens", 700, 200, 4000))


async def _impact(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.impact, workspace, inp.get("changes") or inp.get("diff") or "",
                      max_tokens=_int(inp, "max_tokens", 2200, 300, 10000))


async def _context_pack(inp, *, workspace, instance_id=None, device_tier=None):
    return await _run(ci.context_pack, workspace, inp.get("task") or "", budget_tokens=_int(inp, "budget_tokens", 4000, 300, 40000))


def _spec(name: str) -> toolspec.ToolSpec:
    return toolspec.ToolSpec(name, "read", read_only=True, concurrency_safe=True, max_output_chars=60_000, origin="registered")


def register_all() -> None:
    cheap = " Cheaper than reading whole files: prefer it to read_file for code."
    for schema, handler in (
        ({"name": "outline",
          "description": "Signatures and line ranges of a source file (no bodies), or the top-level names of each file in a folder. "
                         "Use it before read_file on any code file; then read_symbol for the one function you need." + cheap,
          "input_schema": {"type": "object", "properties": {"path": _STR, "max_tokens": _INT}, "required": ["path"]}}, _outline),
        ({"name": "find_symbol",
          "description": "Find functions, classes and methods by exact, qualified or fuzzy name (or a few words from the name or "
                         "docstring). Returns path:start-end, kind and signature. Optional kind: function, method, class, ...",
          "input_schema": {"type": "object", "properties": {"query": _STR, "kind": _STR, "limit": _INT, "max_tokens": _INT},
                           "required": ["query"]}}, _find_symbol),
        ({"name": "read_symbol",
          "description": "The exact source of one symbol: target is a qualified name (module.Class.method, Class.method, name), "
                         "'path::Class.method', or 'path:line' (the innermost symbol containing that line). "
                         "context_lines adds surrounding lines. If the name is ambiguous it lists the candidates." + cheap,
          "input_schema": {"type": "object", "properties": {"target": _STR, "context_lines": _INT, "max_tokens": _INT},
                           "required": ["target"]}}, _read_symbol),
        ({"name": "references",
          "description": "Where a function/class/name is used: uses linked by import first, then by name only. "
                         "Says when the name has several definitions. Best effort, not a type checker.",
          "input_schema": {"type": "object", "properties": {"symbol": _STR, "limit": _INT, "max_tokens": _INT},
                           "required": ["symbol"]}}, _references),
        ({"name": "callers",
          "description": "Which symbols call a function/method/class, with call-site counts. Says when the name is ambiguous.",
          "input_schema": {"type": "object", "properties": {"symbol": _STR, "limit": _INT, "max_tokens": _INT},
                           "required": ["symbol"]}}, _callers),
        ({"name": "related_tests",
          "description": "Tests for a source file or a symbol (by import, reference, naming convention and coverage data), with a "
                         "ready-to-run pytest line.",
          "input_schema": {"type": "object", "properties": {"target": _STR, "max_tokens": _INT}, "required": ["target"]}}, _related_tests),
        ({"name": "impact",
          "description": "Given a unified diff (git diff output) or a list of changed paths: the symbols changed, what calls them, "
                         "the files that import them, and the tests to run.",
          "input_schema": {"type": "object", "properties": {"changes": _STR, "max_tokens": _INT}, "required": ["changes"]}}, _impact),
        ({"name": "context_pack",
          "description": "A budgeted bundle for a task described in words: the relevant signatures, the top function bodies, related "
                         "tests and any project rules - one call instead of many searches and file reads. budget_tokens caps its size.",
          "input_schema": {"type": "object", "properties": {"task": _STR, "budget_tokens": _INT}, "required": ["task"]}}, _context_pack),
    ):
        toolspec.register(schema, _spec(schema["name"]), handler)


register_all()
