"""Parsers for the code index: one interface, several backends.

A parser turns one source file into a `ParsedFile`: its symbols (with exact line ranges and parents), the names it
refers to (with the symbol each reference sits in) and its imports. The index never cares which backend produced
them, so a better parser can replace a weaker one without touching anything else.

Backends, tried in this order (the first that returns a result wins):

* **tree-sitter** - a stub. It switches itself on only when the `tree_sitter` package can be imported, and today it
  declines every file (returns None), so the next backend answers. It is the place to plug in exact parsing for
  every language once the package is approved and installed.
* **python-ast** - the standard `ast` module: exact ranges (decorators included), signatures with defaults and
  annotations, nested classes and functions, async, docstrings, references, imports.
* **regex** - the per-language declaration patterns repo_map already uses (JS/TS, Go, Rust, Java/Kotlin/C#, Swift,
  Ruby, PHP), plus end lines found by brace matching (indentation/`end` for Ruby). A heuristic: it finds
  declarations that start a line and will miss unusual formatting.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from typing import Optional

MAX_SIGNATURE = 240
MAX_DOC = 160

PY_SUFFIXES = {".py", ".pyi"}


@dataclass
class SymbolRec:
    kind: str                    # class | function | method | variable | type | interface | enum | impl | ...
    name: str
    qname: str                   # file-local qualified name: Outer.inner
    signature: str
    doc: str                     # first docstring / comment line
    start: int                   # 1-based, includes decorators
    end: int
    parent: int = -1             # index into ParsedFile.symbols, -1 for top level
    decorators: str = ""


@dataclass
class RefRec:
    name: str
    line: int
    kind: str                    # call | name | attr
    sym: int = -1                # index of the enclosing symbol, -1 for module level


@dataclass
class ImportRec:
    module: str                  # dotted module, or the path/string a non-Python file imports
    name: str = ""               # the imported name for `from m import name`
    level: int = 0               # leading dots of a relative Python import
    line: int = 0


@dataclass
class ParsedFile:
    lang: str
    parser: str
    symbols: list[SymbolRec] = field(default_factory=list)
    refs: list[RefRec] = field(default_factory=list)
    imports: list[ImportRec] = field(default_factory=list)


class Parser:
    name = "base"

    def handles(self, suffix: str) -> bool:
        raise NotImplementedError

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        """A ParsedFile, or None to decline (the next backend is tried)."""
        raise NotImplementedError


# ------------------------------------------------------------------ Python (ast)
_BUILTIN_NAMES = frozenset(dir(builtins)) | {"self", "cls", "super"}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _py_signature(node) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 - an odd node must not lose the symbol
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:  # noqa: BLE001
            ret = ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return _short(f"{prefix} {node.name}({args}){ret}", MAX_SIGNATURE)


def _first_doc_line(node) -> str:
    try:
        doc = ast.get_docstring(node, clean=True) or ""
    except Exception:  # noqa: BLE001
        return ""
    for line in doc.splitlines():
        if line.strip():
            return _short(line.strip(), MAX_DOC)
    return ""


class PythonAstParser(Parser):
    name = "python-ast"

    def handles(self, suffix: str) -> bool:
        return suffix in PY_SUFFIXES

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
        out = ParsedFile(lang="python", parser=self.name)
        seen_refs: set[tuple[str, str, int]] = set()

        def add_ref(name: str, line: int, kind: str, sym: int) -> None:
            if len(name) < 2:
                return
            if kind != "attr" and name in _BUILTIN_NAMES:
                return
            key = (name, kind, sym)
            if key in seen_refs:
                return
            seen_refs.add(key)
            out.refs.append(RefRec(name, line, kind, sym))

        def add_symbol(node, kind: str, name: str, parent: int, signature: str, doc: str) -> int:
            decos = getattr(node, "decorator_list", None) or []
            start = min([node.lineno] + [d.lineno for d in decos])
            qname = name if parent < 0 else f"{out.symbols[parent].qname}.{name}"
            dtext = ""
            if decos:
                try:
                    dtext = _short(", ".join("@" + ast.unparse(d) for d in decos), 160)
                except Exception:  # noqa: BLE001
                    dtext = ""
            out.symbols.append(SymbolRec(kind, name, qname, signature, doc, start,
                                         getattr(node, "end_lineno", None) or node.lineno, parent, dtext))
            return len(out.symbols) - 1

        def walk_children(node, cur: int, in_class: bool) -> None:
            for child in ast.iter_child_nodes(node):
                visit(child, cur, in_class)

        def visit(node, cur: int, in_class: bool) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    visit(deco, cur, False)
                args = node.args
                for d in [*args.defaults, *[x for x in args.kw_defaults if x is not None]]:
                    visit(d, cur, False)
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                    if a is not None and a.annotation is not None:
                        visit(a.annotation, cur, False)
                if node.returns is not None:
                    visit(node.returns, cur, False)
                idx = add_symbol(node, "method" if in_class else "function", node.name, cur,
                                 _py_signature(node), _first_doc_line(node))
                for stmt in node.body:
                    visit(stmt, idx, False)
                return
            if isinstance(node, ast.ClassDef):
                for deco in node.decorator_list:
                    visit(deco, cur, False)
                for b in [*node.bases, *[k.value for k in node.keywords]]:
                    visit(b, cur, False)
                bases = ""
                if node.bases:
                    try:
                        bases = "(" + ", ".join(ast.unparse(b) for b in node.bases) + ")"
                    except Exception:  # noqa: BLE001
                        bases = ""
                idx = add_symbol(node, "class", node.name, cur, _short(f"class {node.name}{bases}", MAX_SIGNATURE),
                                 _first_doc_line(node))
                for stmt in node.body:
                    visit(stmt, idx, True)
                return
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if cur < 0 or in_class:
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1:
                            add_symbol(node, "variable", t.id, cur, _short(t.id, MAX_SIGNATURE), "")
                # fall through: the right-hand side has references
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.imports.append(ImportRec(alias.name, "", 0, node.lineno))
                return
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    out.imports.append(ImportRec(node.module or "", alias.name, node.level or 0, node.lineno))
                return
            elif isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    add_ref(fn.id, node.lineno, "call", cur)
                elif isinstance(fn, ast.Attribute):
                    add_ref(fn.attr, node.lineno, "call", cur)
                    visit(fn.value, cur, False)
                else:
                    visit(fn, cur, False)
                for a in node.args:
                    visit(a, cur, False)
                for k in node.keywords:
                    visit(k.value, cur, False)
                return
            elif isinstance(node, ast.Attribute):
                add_ref(node.attr, node.lineno, "attr", cur)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    add_ref(node.id, node.lineno, "name", cur)
                return
            walk_children(node, cur, in_class if isinstance(node, (ast.If, ast.Try, ast.With)) else False)

        try:
            for stmt in tree.body:
                visit(stmt, -1, False)
        except RecursionError:
            pass            # a pathologically deep expression: keep what was found
        return out


# ------------------------------------------------------------------ regex languages
def _regex_tables():
    # The declaration patterns are repo_map's, so both tools see the same declarations.
    from bot.agent_runtime import repo_map

    return repo_map._C_LIKE, repo_map._PATTERNS


_FUNC_KINDS = {"func", "fn", "def", "method", "function"}
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
_LINE_COMMENT = re.compile(r"//.*$|#.*$")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_NOT_CALLS = frozenset({"if", "for", "while", "switch", "return", "catch", "function", "fn", "func", "def", "class", "new",
                        "sizeof", "typeof", "match", "use", "import", "from", "else", "elif", "with", "assert", "print",
                        "len", "str", "int", "super", "this", "self", "await", "async", "do", "try"})
_IMPORT_PATTERNS = [
    re.compile(r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]"""),         # JS/TS import
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),                          # CommonJS
    re.compile(r"""^\s*import\s+(?:\w+\s+)?"([^"]+)"\s*$"""),                        # Go single import
    re.compile(r"""^\s*\w*\s*"([^"]+)"\s*$"""),                                      # Go grouped import line (filtered below)
    re.compile(r"^\s*use\s+([\w:]+)"),                                               # Rust
    re.compile(r"^\s*import\s+([\w.]+)\s*;?\s*$"),                                   # Java/Kotlin/Swift
    re.compile(r"^\s*using\s+([\w.]+)\s*;"),                                         # C#
]


def _brace_end(lines: list[str], i: int) -> int:
    """0-based index of the line whose brace closes the first brace opened at or after line i; the first line when a
    declaration ends with ';' before any brace."""
    depth = 0
    opened = False
    for j in range(i, min(len(lines), i + 4000)):
        text = _LINE_COMMENT.sub("", _STRING.sub('""', lines[j]))
        for ch in text:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return j
            elif ch == ";" and not opened and depth == 0:
                return j
        if not opened and j >= i + 3:
            return i
    return i if not opened else min(len(lines) - 1, i + 4000)


def _ruby_end(lines: list[str], i: int) -> int:
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped == "end" and len(lines[j]) - len(lines[j].lstrip()) <= indent:
            return j
    return i


class RegexParser(Parser):
    name = "regex"

    def handles(self, suffix: str) -> bool:
        return suffix in _regex_tables()[0]

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        c_like, patterns = _regex_tables()
        group = c_like.get(suffix)
        if group is None:
            return None
        lines = source.split("\n")[:20000]
        out = ParsedFile(lang=group, parser=self.name)
        found: list[tuple[int, int, str, str]] = []        # (start, end, kind, name)
        for i, line in enumerate(lines):
            if len(line) > 300 or not line.strip():
                continue
            for rx, kind in patterns[group]:
                m = rx.match(line)
                if not m:
                    continue
                name = next((g for g in reversed(m.groups()) if g), "")
                if name and name not in ("if", "for", "while", "switch", "return", "catch"):
                    end = _ruby_end(lines, i) if group == "ruby" else _brace_end(lines, i)
                    found.append((i, end, kind, name))
                break
        # parents by containment (found is in start order)
        stack: list[int] = []
        for start, end, kind, name in found:
            while stack and out.symbols[stack[-1]].end < start + 1:
                stack.pop()
            parent = stack[-1] if stack else -1
            k = kind
            if kind in _FUNC_KINDS:
                k = "method" if parent >= 0 and out.symbols[parent].kind not in _FUNC_KINDS | {"function", "method"} else "function"
            qname = name if parent < 0 else f"{out.symbols[parent].qname}.{name}"
            doc = ""
            if start > 0:
                prev = lines[start - 1].strip()
                if prev.startswith(("///", "//", "#", "*", "/**")) and len(prev) > 2:
                    doc = _short(prev.lstrip("/#* ").strip(), MAX_DOC)
            out.symbols.append(SymbolRec(k, name, qname, _short(lines[start].strip().rstrip("{").strip(), MAX_SIGNATURE),
                                         doc, start + 1, max(end, start) + 1, parent))
            stack.append(len(out.symbols) - 1)
        # references: calls by name, with the innermost symbol that contains the line
        ranges = sorted(range(len(out.symbols)), key=lambda s: out.symbols[s].start)
        seen: set[tuple[str, int]] = set()
        decl_lines = {s.start for s in out.symbols}
        for i, line in enumerate(lines):
            if not line.strip() or len(line) > 400:
                continue
            for pat in _IMPORT_PATTERNS:
                m = pat.search(line)
                if m:
                    out.imports.append(ImportRec(m.group(1), "", 0, i + 1))
                    break
            if (i + 1) in decl_lines:
                continue
            text = _STRING.sub('""', line)
            for m in _CALL.finditer(text):
                name = m.group(1)
                if name in _NOT_CALLS or len(name) < 2:
                    continue
                owner = -1
                for s in ranges:
                    sym = out.symbols[s]
                    if sym.start > i + 1:
                        break
                    if sym.end >= i + 1:
                        owner = s          # later (more nested) ranges override
                if (name, owner) in seen:
                    continue
                seen.add((name, owner))
                out.refs.append(RefRec(name, i + 1, "call", owner))
        return out


# ------------------------------------------------------------------ backend selection
def backends() -> list[Parser]:
    """tree-sitter first (when installed), then Python's ast, then the regex extractors."""
    out: list[Parser] = []
    try:
        from bot.codeintel.treesitter import TreeSitterParser

        if TreeSitterParser.available():
            out.append(TreeSitterParser())
    except Exception:  # noqa: BLE001 - a broken install must not take the index down
        pass
    from bot.codeintel.textparsers import MarkdownParser, SashParser

    out += [PythonAstParser(), RegexParser(), SashParser(), MarkdownParser()]
    return out


_BACKENDS: Optional[list[Parser]] = None


def reset_backends() -> None:
    """Forget the chosen backends (tests switch tree-sitter off to exercise the fallbacks)."""
    global _BACKENDS
    _BACKENDS = None


def source_suffixes() -> frozenset[str]:
    from bot.codeintel.textparsers import MARKDOWN_SUFFIXES, SASH_SUFFIXES

    suffixes = set(PY_SUFFIXES) | set(_regex_tables()[0]) | SASH_SUFFIXES | MARKDOWN_SUFFIXES
    try:
        from bot.codeintel.treesitter import SUFFIXES

        suffixes |= set(SUFFIXES)
    except Exception:  # noqa: BLE001
        pass
    return frozenset(suffixes)


def parse_source(suffix: str, source: str, backend: str = "") -> Optional[ParsedFile]:
    """Parse with the first backend that handles the suffix and returns a result (`backend` forces one by name)."""
    global _BACKENDS
    if _BACKENDS is None:
        _BACKENDS = backends()
    suffix = suffix.lower()
    for b in _BACKENDS:
        if backend and b.name != backend:
            continue
        if b.handles(suffix):
            parsed = b.parse(suffix, source)
            if parsed is not None:
                return parsed
    return None
