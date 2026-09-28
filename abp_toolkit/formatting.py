"""Formatting: the project's own formatter for each language when it is installed, built-in formatting otherwise.

Installed formatters used: ruff format (or black), prettier (JS, TS, CSS, HTML, Markdown, YAML, JSON), gofmt, rustfmt,
clang-format, shfmt, PSScriptAnalyzer's Invoke-Formatter, sqlfluff. Built in: JSON, XML/SVG, whitespace (trailing
spaces, final newline, tabs to spaces) and line endings, which work for every text file.
"""
from __future__ import annotations

import difflib
import json
import re
import tempfile
import xml.dom.minidom
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import LANGUAGES, inside, is_binary, language_of, read_text, rel, run, walk, which

group("format", "Format code in any language, normalize whitespace and line endings")

PRETTIER = {"javascript", "typescript", "css", "html", "markdown", "yaml", "json"}


def _external(lang: str, path: Path) -> Optional[list[str]]:
    """The command that formats `path` in place, or None if no formatter for it is installed."""
    if lang == "python":
        if which("ruff"):
            return [which("ruff"), "format", "--quiet", str(path)]
        if which("black"):
            return [which("black"), "--quiet", str(path)]
    if lang in PRETTIER and which("prettier"):
        return [which("prettier"), "--write", "--log-level", "warn", str(path)]
    if lang == "go" and which("gofmt"):
        return [which("gofmt"), "-w", str(path)]
    if lang == "rust" and which("rustfmt"):
        return [which("rustfmt"), "--edition", "2021", str(path)]
    if lang in ("c", "cpp", "csharp", "java") and which("clang-format"):
        return [which("clang-format"), "-i", str(path)]
    if lang == "shell" and which("shfmt"):
        return [which("shfmt"), "-w", "-i", "2", str(path)]
    if lang == "sql" and which("sqlfluff"):
        return [which("sqlfluff"), "fix", "--dialect", "ansi", "--force", "--quiet", str(path)]
    if lang == "powershell" and (which("pwsh") or which("powershell")):
        exe = which("pwsh") or which("powershell")
        script = ("if (-not (Get-Module -ListAvailable PSScriptAnalyzer)) { exit 3 }; "
                  "$p = $args[0]; $t = [IO.File]::ReadAllText($p); $f = Invoke-Formatter -ScriptDefinition $t; "
                  "[IO.File]::WriteAllText($p, $f, (New-Object Text.UTF8Encoding $false))")
        return [exe, "-NoProfile", "-NonInteractive", "-Command", "& {" + script + "}", str(path)]
    return None


def _builtin(lang: Optional[str], text: str, indent: int) -> str:
    if lang == "json":
        try:
            return json.dumps(json.loads(text), indent=indent, ensure_ascii=False) + "\n"
        except ValueError as e:
            raise ToolkitError(f"not valid JSON: {e}") from e
    if lang == "xml":
        try:
            pretty = xml.dom.minidom.parseString(text.encode("utf-8")).toprettyxml(indent=" " * indent)
        except Exception as e:  # noqa: BLE001
            raise ToolkitError(f"not valid XML: {e}") from e
        lines = [l for l in pretty.split("\n") if l.strip()]
        if not text.lstrip().startswith("<?xml"):
            lines = lines[1:]
        return "\n".join(lines) + "\n"
    return whitespace_text(text, tabs_to_spaces=0)


def whitespace_text(text: str, *, tabs_to_spaces: int = 0) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for l in lines:
        l = l.rstrip()
        if tabs_to_spaces:
            m = re.match(r"^[\t ]+", l)
            if m:
                l = m.group(0).replace("\t", " " * tabs_to_spaces) + l[m.end():]
        out.append(l)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _eol_of(raw: bytes) -> str:
    return "\r\n" if raw.count(b"\r\n") > raw.count(b"\n") / 2 else "\n"


def format_file(p: Path, *, check: bool, indent: int = 2) -> Optional[dict]:
    """Format one file (or, with check, only report). Returns {file, tool, diff} if it changed, else None."""
    lang = language_of(p)
    raw = p.read_bytes()
    before = read_text(p)
    eol = _eol_of(raw)
    tool = "builtin"
    cmd = _external(lang, p) if lang else None
    if cmd:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / p.name
            tmp.write_bytes(raw)
            cmd = [c if c != str(p) else str(tmp) for c in cmd]
            r = run(cmd, cwd=p.parent, timeout=120)
            if r.ok:
                after = read_text(tmp)
                tool = Path(cmd[0]).stem
            else:
                cmd = None
    if not cmd:
        after = _builtin(lang, before, indent if lang != "python" else 4)
        if lang not in ("json", "xml"):
            tool = "whitespace"
    if eol == "\r\n":
        after = after.replace("\r\n", "\n").replace("\n", "\r\n")
    if after == before:
        return None
    diff = "".join(difflib.unified_diff(before.replace("\r\n", "\n").splitlines(True), after.replace("\r\n", "\n").splitlines(True),
                                        "before", "after", n=1))
    if not check:
        p.write_bytes(after.encode("utf-8"))
    return {"tool": tool, "diff": diff[:4000]}


@action("format.files", writes=True, needs=["ruff", "prettier", "gofmt", "rustfmt", "clang-format", "shfmt"])
def files(workspace: Path, path: str = ".", check: bool = False, languages: Optional[list[str]] = None,
          max_files: int = 2000) -> dict:
    """Format a file or every file in a folder (check=true only reports what would change, with diffs)

    path: a file or folder inside the working folder
    check: report without changing anything
    languages: only these languages
    """
    target = inside(workspace, path, must_exist=True)
    changed, skipped, errors = [], 0, []
    for f in walk(target, max_files=max_files):
        lang = language_of(f)
        if is_binary(f) or (languages and lang not in languages) or lang is None:
            skipped += 1
            continue
        try:
            r = format_file(f, check=check)
        except ToolkitError as e:
            errors.append({"file": rel(workspace, f), "error": str(e)})
            continue
        if r:
            changed.append({"file": rel(workspace, f), **r})
    return {"checked_only": check, "changed": changed, "unchanged_or_skipped": skipped, "errors": errors}


@action("format.snippet")
def snippet(code: str, language: str, indent: int = 2) -> dict:
    """Format a piece of code and return it (nothing is saved)

    code: the source text
    language: its language (python, json, xml, javascript, go, powershell...)
    indent: spaces per level for the built-in JSON/XML formatter
    """
    ext = {lang: exts[0] for lang, exts in LANGUAGES.items()}
    if language not in ext:
        raise ToolkitError(f"unknown language {language!r}")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ("snippet" + ext[language])
        p.write_bytes(code.encode("utf-8"))
        r = format_file(p, check=False, indent=indent)
        return {"code": read_text(p), "changed": bool(r), "tool": (r or {}).get("tool", "none")}


@action("format.line_endings", writes=True)
def line_endings(workspace: Path, path: str, style: str = "lf", check: bool = False) -> dict:
    """Convert text files to LF or CRLF line endings (binary files are never touched)

    path: a file or folder inside the working folder
    style: lf or crlf
    check: only report which files differ
    """
    if style not in ("lf", "crlf"):
        raise ToolkitError("style is lf or crlf")
    target = inside(workspace, path, must_exist=True)
    changed = []
    for f in walk(target):
        if is_binary(f):
            continue
        raw = f.read_bytes()
        norm = raw.replace(b"\r\n", b"\n")
        new = norm.replace(b"\n", b"\r\n") if style == "crlf" else norm
        if new != raw:
            changed.append(rel(workspace, f))
            if not check:
                f.write_bytes(new)
    return {"style": style, "checked_only": check, "changed": changed}


@action("format.whitespace", writes=True)
def whitespace(workspace: Path, path: str, tabs_to_spaces: int = 0, check: bool = False) -> dict:
    """Remove trailing whitespace, end files with one newline, optionally turn indentation tabs into spaces

    path: a file or folder inside the working folder
    tabs_to_spaces: spaces per tab in indentation (0 = leave tabs)
    check: only report which files would change
    """
    target = inside(workspace, path, must_exist=True)
    changed = []
    for f in walk(target):
        if is_binary(f):
            continue
        raw = f.read_bytes()
        eol = _eol_of(raw)
        text = read_text(f)
        new = whitespace_text(text, tabs_to_spaces=tabs_to_spaces)
        if eol == "\r\n":
            new = new.replace("\n", "\r\n")
        if new != text:
            changed.append(rel(workspace, f))
            if not check:
                f.write_bytes(new.encode("utf-8"))
    return {"checked_only": check, "changed": changed}
