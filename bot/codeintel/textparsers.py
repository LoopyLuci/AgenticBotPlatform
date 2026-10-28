"""Parsers for two languages the tree-sitter pack and repo_map do not cover: Sash (`.sash`) and Markdown.

* **sash** - `fun` / `pub fun` (a method when it sits in a `class`), `class` / `struct` / `enum` / `trait`, end lines by
  brace matching, `import a.b` (resolved by the indexer to `a/b.sash` in the file's folder or an ancestor), calls by
  name, and every `act("name", ...)` entry of an action catalog as an `action` symbol, so an API action is found by
  its name.
* **markdown** - every ATX heading (`#` .. `######`, not inside a code fence) as a `section` symbol spanning up to the
  next heading of the same or a higher level, nested under its parent heading; the first text line under a heading is
  its doc; relative links (`[x](path)`) are imports, so the indexer can resolve them and the catalog can report the
  ones whose target is gone.
"""

from __future__ import annotations

import re
from typing import Optional

from bot.codeintel.parsers import (_CALL, _NOT_CALLS, _STRING, MAX_DOC, MAX_SIGNATURE, ImportRec, ParsedFile, Parser, RefRec,
                                   SymbolRec, _brace_end, _short)

SASH_SUFFIXES = frozenset({".sash"})
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

_SASH_DECL = re.compile(r"^\s*(?:pub\s+)?(fun|class|struct|enum|trait|interface)\s+([A-Za-z_]\w*)")
_SASH_ACTION = re.compile(r"""\bact\(\s*"([A-Za-z_][\w.\-]*)"\s*,\s*"((?:\\.|[^"\\])*)\"""")
_SASH_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)")
_SASH_COMMENT = re.compile(r"#.*$")
_SASH_NOT_CALLS = _NOT_CALLS | {"fun", "act", "append", "string", "split", "assert", "elif"}


class SashParser(Parser):
    name = "sash"

    def handles(self, suffix: str) -> bool:
        return suffix in SASH_SUFFIXES

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        lines = source.split("\n")[:20000]
        out = ParsedFile(lang="sash", parser=self.name)
        found: list[tuple[int, int, str, str, str]] = []        # (start, end, kind, name, signature)
        for i, line in enumerate(lines):
            if len(line) > 400 or not line.strip():
                continue
            m = _SASH_IMPORT.match(line)
            if m:
                out.imports.append(ImportRec(m.group(1), "", 0, i + 1))
                continue
            m = _SASH_DECL.match(line)
            if m:
                found.append((i, _brace_end(lines, i), m.group(1), m.group(2), line.strip().rstrip("{").strip()))
                continue
            for a in _SASH_ACTION.finditer(line):
                found.append((i, i, "action", a.group(1), f'act("{a.group(1)}") {a.group(2)}'))
        stack: list[int] = []
        for start, end, kind, name, sig in found:
            while stack and out.symbols[stack[-1]].end < start + 1:
                stack.pop()
            parent = stack[-1] if stack else -1
            k = kind
            if kind == "fun":
                k = "method" if parent >= 0 and out.symbols[parent].kind in ("class", "struct", "trait", "interface") else "function"
            qname = name if parent < 0 or kind == "action" else f"{out.symbols[parent].qname}.{name}"
            doc = ""
            if start > 0 and kind != "action":
                prev = lines[start - 1].strip()
                if prev.startswith("#") and len(prev) > 1:
                    doc = _short(prev.lstrip("# ").strip(), MAX_DOC)
            out.symbols.append(SymbolRec(k, name, qname, _short(sig, MAX_SIGNATURE), doc, start + 1, max(end, start) + 1,
                                         -1 if kind == "action" else parent))
            if kind != "action":
                stack.append(len(out.symbols) - 1)
        ranges = sorted((s for s in range(len(out.symbols)) if out.symbols[s].kind != "action"),
                        key=lambda s: out.symbols[s].start)
        decl_lines = {s.start for s in out.symbols if s.kind != "action"}
        seen: set[tuple[str, int]] = set()
        for i, line in enumerate(lines):
            if not line.strip() or len(line) > 400:
                continue
            text = _SASH_COMMENT.sub("", _STRING.sub('""', line))
            if (i + 1) in decl_lines:
                text = text.partition("{")[2]            # a one-line body still calls things
            for m in _CALL.finditer(text):
                name = m.group(1)
                if name in _SASH_NOT_CALLS or len(name) < 2:
                    continue
                owner = -1
                for s in ranges:
                    sym = out.symbols[s]
                    if sym.start > i + 1:
                        break
                    if sym.end >= i + 1:
                        owner = s
                if (name, owner) in seen:
                    continue
                seen.add((name, owner))
                out.refs.append(RefRec(name, i + 1, "call", owner))
        return out


_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|#)", re.I)


def link_target(raw: str) -> str:
    """The file part of a Markdown link target ('' for external links and pure anchors)."""
    if _EXTERNAL.match(raw):
        return ""
    return raw.split("#", 1)[0].split("?", 1)[0]


class MarkdownParser(Parser):
    name = "markdown"

    def handles(self, suffix: str) -> bool:
        return suffix in MARKDOWN_SUFFIXES

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        lines = source.split("\n")[:50000]
        out = ParsedFile(lang="markdown", parser=self.name)
        heads: list[tuple[int, int, str]] = []                  # (line index, level, title)
        fence = ""
        for i, line in enumerate(lines):
            f = _FENCE.match(line)
            if f:
                mark = f.group(1)[0]
                if not fence:
                    fence = mark
                elif mark == fence:
                    fence = ""
                continue
            if fence:
                continue
            m = _HEADING.match(line)
            if m:
                heads.append((i, len(m.group(1)), m.group(2).strip()))
            for link in _LINK.finditer(line):
                target = link_target(link.group(1))
                if target:
                    out.imports.append(ImportRec(target, "", 0, i + 1))
        stack: list[int] = []                                   # indexes into out.symbols
        levels: list[int] = []
        for n, (i, level, title) in enumerate(heads):
            end = len(lines) - 1
            for j, lv, _ in heads[n + 1:]:
                if lv <= level:
                    end = j - 1
                    break
            while end > i and not lines[end].strip():
                end -= 1
            while stack and levels[-1] >= level:
                stack.pop()
                levels.pop()
            parent = stack[-1] if stack else -1
            qname = title if parent < 0 else f"{out.symbols[parent].qname} / {title}"
            doc = ""
            for k in range(i + 1, min(end + 1, i + 12)):
                text = lines[k].strip()
                if text and not _HEADING.match(lines[k]) and not _FENCE.match(lines[k]):
                    doc = _short(text, MAX_DOC)
                    break
            out.symbols.append(SymbolRec("section", title, _short(qname, MAX_SIGNATURE),
                                         _short("#" * level + " " + title, MAX_SIGNATURE), doc, i + 1, end + 1, parent))
            stack.append(len(out.symbols) - 1)
            levels.append(level)
        return out
