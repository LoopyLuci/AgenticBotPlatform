"""A linter for every language: the best installed tool for each, and built-in checks that always work.

For each file it finds, the linter runs the real tools a project would use when they are installed (ruff, eslint,
tsc, shellcheck, PSScriptAnalyzer, go vet, hadolint, yamllint, rubocop, php -l, luacheck, cppcheck, stylelint...)
and its own checks otherwise: a syntax check for every language it can parse (Python, JSON, YAML, TOML, XML/SVG,
INI, HTML, CSS, PowerShell through its own parser, JavaScript through node, shell through bash), structural checks for
the languages without a checker (batch files, VBScript, SQL, CSV, Dockerfiles), and checks every text file gets:
merge-conflict markers, mixed line endings, mixed tabs and spaces, secrets committed by mistake, very long lines.

Every finding has the same shape: file, line, column, severity (error / warning / info), code, message, tool.
"""
from __future__ import annotations

import ast
import configparser
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, is_binary, language_of, read_text, rel, run, walk, which

group("lint", "Lint any language: the installed linters plus built-in syntax, structure and hygiene checks")

SEVERITIES = ("error", "warning", "info")
INSTALL_HINTS = {
    "ruff": "pip install ruff", "eslint": "npm install -g eslint", "tsc": "npm install -g typescript",
    "shellcheck": "scoop install shellcheck / apt install shellcheck", "hadolint": "scoop install hadolint",
    "yamllint": "pip install yamllint", "golangci-lint": "go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest",
    "rubocop": "gem install rubocop", "luacheck": "luarocks install luacheck", "cppcheck": "scoop install cppcheck",
    "stylelint": "npm install -g stylelint stylelint-config-standard", "markdownlint": "npm install -g markdownlint-cli",
    "node": "https://nodejs.org", "go": "https://go.dev/dl", "php": "https://www.php.net/downloads",
    "sqlfluff": "pip install sqlfluff", "mypy": "pip install mypy", "PSScriptAnalyzer": "Install-Module PSScriptAnalyzer",
}


@dataclass
class Finding:
    file: str
    line: int
    column: int
    severity: str
    code: str
    message: str
    tool: str


# ---- the built-in checks --------------------------------------------------------------------------------------------
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("password-assignment", re.compile(r"""(?i)\b(?:password|passwd|pwd|secret|api_?key)\s*[:=]\s*["'][^"'\s]{8,}["']""")),
]


def hygiene(path: Path, text: str, name: str, max_line: int) -> list[Finding]:
    out: list[Finding] = []
    lines = text.split("\n")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        out.append(Finding(name, 1, 1, "warning", "mixed-eol", f"mixed line endings ({crlf} CRLF, {lf} LF)", "builtin"))
    tab_indent = space_indent = 0
    for i, line in enumerate(lines, 1):
        s = line.rstrip("\r")
        if s.startswith(("<<<<<<< ", ">>>>>>> ")) or s == "=======" and 0 < i < len(lines):
            if s != "=======" or any(l.startswith("<<<<<<< ") for l in lines[max(0, i - 400):i]):
                out.append(Finding(name, i, 1, "error", "merge-conflict", "merge conflict marker", "builtin"))
        if s.startswith("\t"):
            tab_indent += 1
        elif s.startswith("    "):
            space_indent += 1
        if max_line and len(s) > max_line and not s.lstrip().startswith(("http", "data:")):
            out.append(Finding(name, i, max_line + 1, "info", "long-line", f"line is {len(s)} characters", "builtin"))
        for code, pat in SECRET_PATTERNS:
            m = pat.search(s)
            if m and "example" not in s.lower() and "placeholder" not in s.lower():
                out.append(Finding(name, i, m.start() + 1, "error", f"secret/{code}",
                                   "looks like a secret committed in the file (move it to an environment variable or a vault)",
                                   "builtin"))
    if tab_indent and space_indent and path.suffix.lower() not in (".go", ".mk") and path.name != "Makefile":
        out.append(Finding(name, 1, 1, "warning", "mixed-indent",
                           f"indented with tabs on {tab_indent} line(s) and spaces on {space_indent}", "builtin"))
    return out


class _PyChecks(ast.NodeVisitor):
    """Checks pyflakes-less Python still gets: unused imports, mutable defaults, bare except, comparisons to None."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.found: list[Finding] = []
        self.imported: dict[str, tuple[int, int]] = {}
        self.used: set[str] = set()

    def add(self, node: ast.AST, severity: str, code: str, msg: str) -> None:
        self.found.append(Finding(self.name, getattr(node, "lineno", 1), getattr(node, "col_offset", 0) + 1,
                                  severity, code, msg, "builtin"))

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.imported[(a.asname or a.name).split(".")[0]] = (node.lineno, node.col_offset + 1)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "__future__":
            return
        for a in node.names:
            if a.name != "*":
                self.imported[a.asname or a.name] = (node.lineno, node.col_offset + 1)

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            self.used.add(root.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node) -> None:
        for d in node.args.defaults + node.args.kw_defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.add(d, "warning", "B006", "mutable default argument: it is shared between calls (use None)")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add(node, "warning", "E722", "bare except: also catches KeyboardInterrupt and SystemExit")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
                self.add(node, "warning", "E711", "compare to None with 'is' / 'is not'")
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        if not any(isinstance(v, ast.FormattedValue) for v in node.values):
            self.add(node, "info", "F541", "f-string without any placeholders")
        self.generic_visit(node)

    def finish(self, text: str) -> list[Finding]:
        exported = set(re.findall(r"""['"](\w+)['"]""", text.split("__all__", 1)[1])) if "__all__" in text else set()
        for name, (line, col) in self.imported.items():
            if name not in self.used and name not in exported and not self.name.endswith("__init__.py"):
                self.found.append(Finding(self.name, line, col, "warning", "F401", f"'{name}' imported but unused", "builtin"))
        return self.found


def _check_python(p: Path, text: str, name: str) -> list[Finding]:
    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError as e:
        return [Finding(name, e.lineno or 1, e.offset or 1, "error", "syntax", e.msg, "builtin")]
    v = _PyChecks(name)
    v.visit(tree)
    return v.finish(text)


def _check_json(p: Path, text: str, name: str) -> list[Finding]:
    body = text
    if p.suffix.lower() in (".jsonc", ".json5") or p.name in ("tsconfig.json", "jsconfig.json", ".eslintrc.json") or \
            p.parent.name == ".vscode":
        body = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
        body = re.sub(r",(\s*[}\]])", r"\1", body)
    try:
        json.loads(body)
        return []
    except json.JSONDecodeError as e:
        return [Finding(name, e.lineno, e.colno, "error", "syntax", e.msg, "builtin")]


def _check_yaml(p: Path, text: str, name: str) -> list[Finding]:
    try:
        import yaml
    except ImportError:
        return []

    class Loader(yaml.SafeLoader):
        pass
    # CloudFormation / GitHub Actions / Ansible tags are not errors
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    try:
        list(yaml.load_all(text, Loader=Loader))
        return []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return [Finding(name, (mark.line + 1) if mark else 1, (mark.column + 1) if mark else 1, "error", "syntax",
                        str(getattr(e, "problem", None) or e).strip(), "builtin")]


def _check_toml(p: Path, text: str, name: str) -> list[Finding]:
    try:
        import tomllib
    except ImportError:
        return []
    try:
        tomllib.loads(text)
        return []
    except tomllib.TOMLDecodeError as e:
        m = re.search(r"line (\d+), column (\d+)", str(e))
        return [Finding(name, int(m.group(1)) if m else 1, int(m.group(2)) if m else 1, "error", "syntax",
                        str(e).split(" (at")[0], "builtin")]


def _check_xml(p: Path, text: str, name: str) -> list[Finding]:
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(text.lstrip("\ufeff").encode("utf-8"))
        return []
    except ET.ParseError as e:
        line, col = getattr(e, "position", (1, 0))
        return [Finding(name, line, col + 1, "error", "syntax", str(e).split(":")[0], "builtin")]


def _check_ini(p: Path, text: str, name: str) -> list[Finding]:
    if p.name in (".editorconfig",) or p.suffix.lower() == ".conf":
        return []
    cp = configparser.ConfigParser(strict=True, interpolation=None)
    try:
        cp.read_string(text if text.lstrip().startswith("[") else "[__root__]\n" + text)
        return []
    except configparser.Error as e:
        m = re.search(r"line (\d+)", str(e))
        return [Finding(name, int(m.group(1)) if m else 1, 1, "error", "syntax", str(e).splitlines()[0], "builtin")]


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
OPTIONAL_END = {"p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody", "tfoot", "option", "colgroup", "rt", "rp",
                "html", "head", "body", "caption", "optgroup"}


class _HtmlBalance(HTMLParser):
    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name
        self.stack: list[tuple[str, int, int]] = []
        self.found: list[Finding] = []
        self.ids: dict[str, int] = {}
        self.has_alt_issue = False

    def handle_starttag(self, tag, attrs):
        line, col = self.getpos()
        a = dict(attrs)
        if a.get("id"):
            if a["id"] in self.ids:
                self.found.append(Finding(self.name, line, col + 1, "warning", "duplicate-id",
                                          f"id \"{a['id']}\" is also used on line {self.ids[a['id']]}", "builtin"))
            self.ids[a["id"]] = line
        if tag == "img" and "alt" not in a:
            self.found.append(Finding(self.name, line, col + 1, "warning", "img-alt", "<img> without alt text", "builtin"))
        if tag not in VOID:
            self.stack.append((tag, line, col + 1))

    def handle_endtag(self, tag):
        line, col = self.getpos()
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, l, c in self.stack[i + 1:]:
                    if t not in OPTIONAL_END:
                        self.found.append(Finding(self.name, l, c, "error", "unclosed-tag", f"<{t}> is never closed", "builtin"))
                del self.stack[i:]
                return
        self.found.append(Finding(self.name, line, col + 1, "error", "stray-end-tag", f"</{tag}> has no opening tag", "builtin"))

    def finish(self) -> list[Finding]:
        for t, l, c in self.stack:
            if t not in OPTIONAL_END:
                self.found.append(Finding(self.name, l, c, "error", "unclosed-tag", f"<{t}> is never closed", "builtin"))
        return self.found


def _check_html(p: Path, text: str, name: str) -> list[Finding]:
    h = _HtmlBalance(name)
    h.feed(re.sub(r"<(script|style)\b[^>]*>.*?</\1>", lambda m: "<%s></%s>" % (m.group(1), m.group(1)) + "\n" * m.group(0).count("\n"),
                  text, flags=re.S | re.I))
    h.close()
    return h.finish()


def _balance(text: str, name: str, pairs: dict[str, str], *, strip: Callable[[str], str] = lambda s: s,
             code: str = "unbalanced") -> list[Finding]:
    """Brackets that are opened and not closed (or the reverse), outside strings and comments."""
    closers = {v: k for k, v in pairs.items()}
    stack: list[tuple[str, int, int]] = []
    for ln, line in enumerate(strip(text).split("\n"), 1):
        for col, ch in enumerate(line, 1):
            if ch in pairs:
                stack.append((ch, ln, col))
            elif ch in closers:
                if not stack or stack[-1][0] != closers[ch]:
                    return [Finding(name, ln, col, "error", code, f"'{ch}' without a matching '{closers[ch]}'", "builtin")]
                stack.pop()
    return [Finding(name, l, c, "error", code, f"'{ch}' is never closed", "builtin") for ch, l, c in stack[:3]]


def _strip_c_like(text: str) -> str:
    """Comments and string literals blanked out (line structure kept)."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`', blank, text, flags=re.S)


def _check_css(p: Path, text: str, name: str) -> list[Finding]:
    return _balance(text, name, {"{": "}", "(": ")", "[": "]"}, strip=_strip_c_like)


def _check_batch(p: Path, text: str, name: str) -> list[Finding]:
    out: list[Finding] = []
    lines = text.replace("\r\n", "\n").split("\n")
    labels = {l.strip()[1:].split()[0].lower() for l in lines if l.strip().startswith(":") and not l.strip().startswith("::")
              and len(l.strip()) > 1}
    labels.add("eof")
    depth = 0
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        low = s.lower()
        if not s or low.startswith(("rem ", "::")) or low == "rem":
            continue
        for m in re.finditer(r"\b(?:goto|call)\s+:?([\w.-]+)", s, re.I):
            target = m.group(1).lower()
            if (m.group(0).lower().startswith("goto") or ":" in m.group(0)) and target not in labels and not target.startswith("%"):
                out.append(Finding(name, i, m.start() + 1, "error", "missing-label", f"label :{m.group(1)} is not defined", "builtin"))
        quoted = re.sub(r'"[^"]*"', "", s)
        quoted = re.sub(r"\^.", "", quoted)
        depth += quoted.count("(") - quoted.count(")")
        if depth < 0:
            out.append(Finding(name, i, 1, "error", "unbalanced", "')' closes a block that was never opened", "builtin"))
            depth = 0
        if re.match(r"(?i)set\s+\w+\s+=", s):
            out.append(Finding(name, i, 1, "warning", "set-space", "space before '=' makes it part of the variable's name", "builtin"))
    if depth > 0:
        out.append(Finding(name, len(lines), 1, "error", "unbalanced", f"{depth} '(' block(s) never closed", "builtin"))
    if "setlocal" not in text.lower() and re.search(r"(?im)^\s*set\s", text):
        out.append(Finding(name, 1, 1, "info", "no-setlocal", "variables leak into the caller's session without SETLOCAL", "builtin"))
    return out


VB_BLOCKS = [("if", r"^if\b.*\bthen\s*(?:'.*)?$", r"^end\s+if\b"), ("sub", r"^(?:(?:public|private)\s+)?sub\b", r"^end\s+sub\b"),
             ("function", r"^(?:(?:public|private)\s+)?function\b", r"^end\s+function\b"), ("for", r"^for\b", r"^next\b"),
             ("do", r"^do\b", r"^loop\b"), ("while", r"^while\b", r"^wend\b"), ("select", r"^select\s+case\b", r"^end\s+select\b"),
             ("with", r"^with\b", r"^end\s+with\b"), ("class", r"^class\b", r"^end\s+class\b"),
             ("property", r"^(?:(?:public|private)\s+)?property\b", r"^end\s+property\b")]


def _check_vbscript(p: Path, text: str, name: str) -> list[Finding]:
    out: list[Finding] = []
    stack: list[tuple[str, int]] = []
    for i, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        s = re.sub(r'"[^"]*"', '""', raw).split("'", 1)[0].strip()
        low = s.lower()
        if not low or low.startswith("rem "):
            continue
        for part in low.split(":"):
            part = part.strip()
            for kind, open_re, close_re in VB_BLOCKS:
                if re.match(close_re, part):
                    depth = next((d for d in range(len(stack) - 1, -1, -1) if stack[d][0] == kind), None)
                    if depth is None:
                        out.append(Finding(name, i, 1, "error", "unbalanced",
                                           f"'{' '.join(part.split()[:2])}' closes a {kind} that is not open", "builtin"))
                    else:
                        # Blocks opened inside it and never closed: report each once, at the line that opened it.
                        for inner, line in stack[depth + 1:]:
                            out.append(Finding(name, line, 1, "error", "unclosed-block",
                                               f"{inner} block is never closed (its {kind} ends on line {i})", "builtin"))
                        del stack[depth:]
                    break
                if re.match(open_re, part) and not (kind == "if" and re.search(r"\bthen\s+\S", part)) and \
                        not (kind == "property" and re.match(r"^(?:(?:public|private)\s+)?property\s+(?:get|let|set)\b", part) is None and "=" in part):
                    stack.append((kind, i))
                    break
    for kind, line in stack:
        out.append(Finding(name, line, 1, "error", "unclosed-block", f"{kind} block is never closed", "builtin"))
    if not re.search(r"(?im)^\s*option\s+explicit\b", text) and p.suffix.lower() == ".vbs":
        out.append(Finding(name, 1, 1, "warning", "option-explicit", "without Option Explicit a misspelled variable is silently a new one", "builtin"))
    return out


def _check_sql(p: Path, text: str, name: str) -> list[Finding]:
    stripped = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    out = _balance(stripped, name, {"(": ")"})
    for m in re.finditer(r"(?is)\b(update|delete\s+from)\s+\w+[^;]*?;", stripped):
        if not re.search(r"(?i)\bwhere\b", m.group(0)):
            out.append(Finding(name, stripped[:m.start()].count("\n") + 1, 1, "warning", "no-where",
                               f"{m.group(1).upper()} without WHERE changes every row", "builtin"))
    return out


def _check_csv(p: Path, text: str, name: str) -> list[Finding]:
    import csv
    import io
    delim = "\t" if p.suffix.lower() == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return []
    width = len(rows[0])
    return [Finding(name, i, 1, "warning", "column-count", f"{len(r)} columns where the header has {width}", "builtin")
            for i, r in enumerate(rows, 1) if r and len(r) != width][:20]


def _check_dockerfile(p: Path, text: str, name: str) -> list[Finding]:
    out: list[Finding] = []
    lines = text.split("\n")
    first = next((i for i, l in enumerate(lines, 1) if l.strip() and not l.strip().startswith("#")), 0)
    if first and not re.match(r"(?i)^\s*(from|arg)\b", lines[first - 1]):
        out.append(Finding(name, first, 1, "error", "DL3061", "a Dockerfile must start with FROM (or ARG)", "builtin"))
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if re.match(r"(?i)^from\s+[^\s:@]+(?:\s|$)", s) and "scratch" not in s.lower() and not re.search(r"\$\{?\w", s):
            out.append(Finding(name, i, 1, "warning", "DL3006", "pin the image version (no tag means :latest)", "builtin"))
        if re.match(r"(?i)^from\s+\S+:latest\b", s):
            out.append(Finding(name, i, 1, "warning", "DL3007", "the :latest tag changes under you; pin a version", "builtin"))
        if re.search(r"apt-get\s+install(?!.*-y)", s):
            out.append(Finding(name, i, 1, "warning", "DL3014", "apt-get install without -y waits for an answer", "builtin"))
        if re.match(r"(?i)^add\s+(?!https?://)", s) and not re.search(r"\.(tar|gz|tgz|bz2|xz)\b", s):
            out.append(Finding(name, i, 1, "info", "DL3020", "use COPY for local files (ADD also unpacks and downloads)", "builtin"))
    return out


def _check_markdown(p: Path, text: str, name: str) -> list[Finding]:
    out: list[Finding] = []
    root = p.parent
    fence = False
    for i, l in enumerate(text.split("\n"), 1):
        if l.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        for m in re.finditer(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", l):
            target = m.group(1)
            if re.match(r"^[a-z]+:", target) or target.startswith("/"):
                continue
            if not (root / target).exists():
                out.append(Finding(name, i, m.start() + 1, "warning", "broken-link", f"link to {target}: no such file", "builtin"))
    if fence:
        out.append(Finding(name, len(text.split("\n")), 1, "error", "unclosed-fence", "a ``` code block is never closed", "builtin"))
    return out


def _check_c_like(p: Path, text: str, name: str) -> list[Finding]:
    return _balance(text, name, {"{": "}", "(": ")", "[": "]"}, strip=_strip_c_like)


BUILTIN: dict[str, Callable[[Path, str, str], list[Finding]]] = {
    "python": _check_python, "json": _check_json, "yaml": _check_yaml, "toml": _check_toml, "xml": _check_xml,
    "ini": _check_ini, "html": _check_html, "css": _check_css, "batch": _check_batch, "vbscript": _check_vbscript,
    "sql": _check_sql, "csv": _check_csv, "dockerfile": _check_dockerfile, "markdown": _check_markdown,
    "go": _check_c_like, "rust": _check_c_like, "c": _check_c_like, "cpp": _check_c_like, "csharp": _check_c_like,
    "java": _check_c_like, "kotlin": _check_c_like, "swift": _check_c_like, "php": _check_c_like, "dart": _check_c_like,
    "scala": _check_c_like, "javascript": _check_c_like, "typescript": _check_c_like,
}


# ---- external linters -------------------------------------------------------------------------------------------------
def _parse_regex(output: str, pattern: str, tool: str, root: Path, default_sev: str = "warning") -> list[Finding]:
    out = []
    for m in re.finditer(pattern, output, re.M):
        g = m.groupdict()
        sev = (g.get("sev") or default_sev).lower()
        sev = "error" if sev.startswith(("e", "f")) else "info" if sev.startswith(("i", "n", "s", "c")) and sev != "c" else "warning" if sev else default_sev
        out.append(Finding(rel(root, Path(g.get("file") or "")) if g.get("file") else "", int(g.get("line") or 1),
                           int(g.get("col") or 1), sev, g.get("code") or "", (g.get("msg") or "").strip(), tool))
    return out


def ext_ruff(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("ruff")
    if not exe:
        return [], "ruff"
    r = run([exe, "check", "--output-format", "json", "--no-cache", *(["--fix"] if fix else []), *files], cwd=root, timeout=300)
    try:
        items = json.loads(r.out or "[]")
    except ValueError:
        return [], ""
    return [Finding(rel(root, Path(i["filename"])), i["location"]["row"], i["location"]["column"],
                    "error" if (i.get("code") or "invalid-syntax").startswith(("E9", "F8", "invalid-syntax")) else "warning",
                    i.get("code") or "syntax", i["message"], "ruff") for i in items], ""


def ext_mypy(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("mypy")
    if not exe:
        return [], ""        # optional: not reported missing, ruff and the built-ins already cover Python
    r = run([exe, "--no-error-summary", "--show-column-numbers", "--ignore-missing-imports", "--no-color-output", *files], cwd=root, timeout=600)
    return _parse_regex(r.out, r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning|note): (?P<msg>.+?)(?:\s+\[(?P<code>[\w-]+)\])?$", "mypy", root), ""


def ext_eslint(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("eslint")
    if not exe:
        return [], "eslint"
    r = run([exe, "--format", "json", *(["--fix"] if fix else []), *files], cwd=root, timeout=600)
    try:
        items = json.loads(r.out or "[]")
    except ValueError:
        return [], ""
    return [Finding(rel(root, Path(f["filePath"])), m.get("line", 1), m.get("column", 1),
                    "error" if m.get("severity") == 2 else "warning", m.get("ruleId") or "syntax", m["message"], "eslint")
            for f in items for m in f.get("messages", [])], ""


def ext_node_check(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("node")
    if not exe:
        return [], "node"
    out = []
    for f in files:
        if f.suffix.lower() not in (".js", ".mjs", ".cjs"):
            continue
        r = run([exe, "--check", f], cwd=root, timeout=30)
        if not r.ok:
            m = re.search(r":(\d+)\n", r.err)
            msg = next((l for l in r.err.splitlines() if "Error" in l), r.err.strip()[:200])
            out.append(Finding(rel(root, f), int(m.group(1)) if m else 1, 1, "error", "syntax", msg, "node"))
    return out, ""


def ext_tsc(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("tsc")
    if not exe:
        return [], "tsc"
    project = next((p for p in [root / "tsconfig.json"] if p.exists()), None)
    args = [exe, "--noEmit", "--pretty", "false", *(["-p", project] if project else ["--allowJs", "--skipLibCheck", *files])]
    r = run(args, cwd=root, timeout=900)
    return _parse_regex(r.out, r"^(?P<file>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) (?P<code>TS\d+): (?P<msg>.+)$", "tsc", root), ""


def ext_shellcheck(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("shellcheck")
    if not exe:
        bash = which("bash")
        out = []
        for f in files if bash else []:
            r = run([bash, "-n", f], cwd=root, timeout=30)
            if not r.ok:
                m = re.search(r"line (\d+): (.+)", r.err)
                out.append(Finding(rel(root, f), int(m.group(1)) if m else 1, 1, "error", "syntax",
                                   m.group(2) if m else r.err.strip()[:200], "bash -n"))
        return out, "shellcheck"
    r = run([exe, "-f", "json", *files], cwd=root, timeout=300)
    try:
        items = json.loads(r.out or "[]")
    except ValueError:
        return [], ""
    return [Finding(rel(root, Path(i["file"])), i["line"], i["column"], {"error": "error", "warning": "warning"}.get(i["level"], "info"),
                    f"SC{i['code']}", i["message"], "shellcheck") for i in items], ""


PS_PARSE = r"""
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($f in $args) {
  $tokens = $null; $errs = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$tokens, [ref]$errs)
  foreach ($e in $errs) { $out += [pscustomobject]@{file=$f; line=$e.Extent.StartLineNumber; col=$e.Extent.StartColumnNumber; sev='error'; code=$e.ErrorId; msg=$e.Message; tool='powershell-parser'} }
}
if (Get-Module -ListAvailable -Name PSScriptAnalyzer) {
  foreach ($f in $args) {
    foreach ($d in (Invoke-ScriptAnalyzer -Path $f)) { $out += [pscustomobject]@{file=$f; line=$d.Line; col=$d.Column; sev=[string]$d.Severity; code=$d.RuleName; msg=$d.Message; tool='PSScriptAnalyzer'} }
  }
} else { $out += [pscustomobject]@{file=''; line=0; col=0; sev='missing'; code='PSScriptAnalyzer'; msg=''; tool=''} }
ConvertTo-Json -InputObject @($out) -Depth 3 -Compress
"""


def ext_powershell(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("pwsh") or which("powershell")
    if not exe:
        return [], "pwsh"
    r = run([exe, "-NoProfile", "-NonInteractive", "-Command", "& {" + PS_PARSE + "}", *files], cwd=root, timeout=600)
    try:
        items = json.loads(r.out.strip() or "[]")
    except ValueError:
        return [], ""
    missing = ""
    out = []
    for i in items if isinstance(items, list) else [items]:
        if i.get("sev") == "missing":
            missing = "PSScriptAnalyzer"
            continue
        sev = str(i.get("sev", "")).lower()
        out.append(Finding(rel(root, Path(i["file"])), int(i.get("line") or 1), int(i.get("col") or 1),
                           "error" if sev in ("error", "parseerror") else "info" if sev == "information" else "warning",
                           str(i.get("code") or ""), str(i.get("msg") or ""), str(i.get("tool") or "powershell")))
    return out, missing


def ext_go(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("go")
    if not exe:
        return [], "go"
    if not (root / "go.mod").exists():
        r = run([which("gofmt") or exe, "-e", "-l", *files] if which("gofmt") else [exe, "vet", *files], cwd=root, timeout=300)
        return _parse_regex(r.err, r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+)$", "gofmt", root, "error"), ""
    r = run([exe, "vet", "./..."], cwd=root, timeout=900)
    return _parse_regex(r.err, r"^(?:vet: )?(?P<file>[^:\n]+\.go):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+)$", "go vet", root), ""


def ext_cargo(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
    exe = which("cargo")
    if not exe or not (root / "Cargo.toml").exists():
        return [], "" if not exe else ""
    r = run([exe, "clippy", "--message-format", "short", "--quiet"], cwd=root, timeout=1800)
    return _parse_regex(r.err, r"^(?P<file>[^:\n]+\.rs):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning)(?:\[(?P<code>[^\]]+)\])?: (?P<msg>.+)$", "clippy", root), ""


def _simple(tool: str, args: Callable[[str, list[Path]], list], pattern: str, *, stream: str = "out",
            default_sev: str = "warning", timeout: float = 300):
    def fn(files: list[Path], root: Path, fix: bool) -> tuple[list[Finding], str]:
        exe = which(tool)
        if not exe:
            return [], tool
        r = run(args(exe, files), cwd=root, timeout=timeout)
        return _parse_regex(r.out if stream == "out" else r.err + r.out, pattern, tool, root, default_sev), ""
    return fn


EXTERNAL: dict[str, list[Callable]] = {
    "python": [ext_ruff, ext_mypy],
    "javascript": [ext_eslint, ext_node_check],
    "typescript": [ext_tsc, ext_eslint],
    "shell": [ext_shellcheck],
    "powershell": [ext_powershell],
    "go": [ext_go],
    "rust": [ext_cargo],
    "yaml": [_simple("yamllint", lambda e, f: [e, "-f", "parsable", *f],
                     r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): \[(?P<sev>\w+)\] (?P<msg>.+?)(?: \((?P<code>[\w-]+)\))?$")],
    "dockerfile": [_simple("hadolint", lambda e, f: [e, "--format", "tty", "--no-color", *f],
                           r"^(?P<file>[^:\n]+):(?P<line>\d+) (?P<code>\w+) (?P<sev>\w+): (?P<msg>.+)$")],
    "ruby": [_simple("rubocop", lambda e, f: [e, "--format", "emacs", *f],
                     r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<sev>[CWEF]): (?:\[Correctable\] )?(?P<code>[\w/]+): (?P<msg>.+)$")],
    "php": [_simple("php", lambda e, f: [e, "-l", *f[:1]], r"(?:PHP )?(?P<sev>Parse error|Fatal error): (?P<msg>.+?) in (?P<file>.+?) on line (?P<line>\d+)",
                    default_sev="error")],
    "lua": [_simple("luacheck", lambda e, f: [e, "--formatter", "plain", "--codes", *f],
                    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): \((?P<code>[EW]\d+)\) (?P<msg>.+)$")],
    "c": [_simple("cppcheck", lambda e, f: [e, "--quiet", "--enable=warning,style,performance", "--template={file}:{line}:{column}: {severity}: {id}: {message}", *f],
                  r"^(?P<file>[^:\n]+(?::[^:\n]+)?):(?P<line>\d+):(?P<col>\d+): (?P<sev>\w+): (?P<code>\w+): (?P<msg>.+)$", stream="err")],
    "cpp": [_simple("cppcheck", lambda e, f: [e, "--quiet", "--enable=warning,style,performance", "--template={file}:{line}:{column}: {severity}: {id}: {message}", *f],
                    r"^(?P<file>[^:\n]+(?::[^:\n]+)?):(?P<line>\d+):(?P<col>\d+): (?P<sev>\w+): (?P<code>\w+): (?P<msg>.+)$", stream="err")],
    "css": [_simple("stylelint", lambda e, f: [e, "--formatter", "unix", *f],
                    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+?) \[(?P<sev>error|warning)\]$")],
    "markdown": [_simple("markdownlint", lambda e, f: [e, *f],
                         r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<col>\d+))? (?P<code>MD\d+)/\S+ (?P<msg>.+)$", stream="err")],
    "sql": [_simple("sqlfluff", lambda e, f: [e, "lint", "--dialect", "ansi", "--format", "github-annotation-native", *f],
                    r"^::(?P<sev>error|warning|notice) title=SQLFluff,file=(?P<file>[^,]+),line=(?P<line>\d+),col=(?P<col>\d+)[^:]*::(?P<code>\w+): (?P<msg>.+)$")],
}


# ---- the action -------------------------------------------------------------------------------------------------------
def lint_paths(workspace: Path, path: str = ".", languages: Optional[list[str]] = None, *, fix: bool = False,
               external: bool = True, hygiene_checks: bool = True, max_line: int = 200, max_findings: int = 500,
               min_severity: str = "info") -> dict:
    target = inside(workspace, path, must_exist=True)
    files = [f for f in walk(target) if not is_binary(f)]
    by_lang: dict[str, list[Path]] = {}
    other: list[Path] = []
    for f in files:
        lang = language_of(f)
        if languages and lang not in languages:
            continue
        (by_lang.setdefault(lang, []) if lang else other).append(f)
    findings: list[Finding] = []
    used: set[str] = set()
    missing: set[str] = set()
    root = Path(workspace).resolve()
    for lang, lfiles in sorted(by_lang.items()):
        covered_syntax = False
        if external:
            for fn in EXTERNAL.get(lang, []):
                try:
                    got, miss = fn(lfiles, root, fix)
                except Exception as e:  # noqa: BLE001 - one broken linter must not stop the others
                    got, miss = [Finding("", 1, 1, "info", "linter-failed", f"{fn.__name__}: {e}", "toolkit")], ""
                if miss:
                    missing.add(miss)
                if got or not miss:
                    tool_names = {g.tool for g in got} or {fn.__name__.replace("ext_", "") if fn.__name__ != "fn" else lang}
                    used.update(tool_names)
                    covered_syntax = covered_syntax or not miss
                findings.extend(got)
        check = BUILTIN.get(lang)
        # The built-in syntax check runs unless a real parser for the language already did (ruff parses Python,
        # eslint/tsc parse JS/TS). Built-in structure checks for other languages always run.
        if check and not (covered_syntax and lang in ("python", "javascript", "typescript")):
            used.add("builtin")
            for f in lfiles:
                try:
                    findings.extend(check(f, read_text(f), rel(root, f)))
                except Exception as e:  # noqa: BLE001
                    findings.append(Finding(rel(root, f), 1, 1, "info", "check-failed", str(e)[:200], "builtin"))
    if hygiene_checks:
        for f in [x for fs in by_lang.values() for x in fs] + other:
            try:
                findings.extend(hygiene(f, read_text(f), rel(root, f), max_line))
            except Exception:  # noqa: BLE001
                pass
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    findings = [x for x in findings if rank.get(x.severity, 2) <= rank.get(min_severity, 2)]
    findings.sort(key=lambda x: (rank.get(x.severity, 2), x.file, x.line))
    counts = {s: sum(1 for x in findings if x.severity == s) for s in SEVERITIES}
    return {"files": len(files), "languages": {k: len(v) for k, v in sorted(by_lang.items())}, "tools_used": sorted(used),
            "tools_missing": {m: INSTALL_HINTS.get(m, "") for m in sorted(missing)}, "counts": counts,
            "findings": findings[:max_findings], "truncated": len(findings) > max_findings, "fixed": fix}


@action("lint.check", needs=["ruff", "eslint", "tsc", "shellcheck", "PSScriptAnalyzer", "hadolint", "yamllint"])
def check(workspace: Path, path: str = ".", languages: Optional[list[str]] = None, min_severity: str = "info",
          max_findings: int = 500, external: bool = True, max_line: int = 200) -> dict:
    """Lint a file or folder in any language and report every finding in one format

    path: a file or folder inside the working folder
    languages: only these (python, javascript, typescript, powershell, batch, vbscript, shell, json, yaml, ...)
    min_severity: error, warning or info
    external: also run the installed linters (ruff, eslint, tsc, shellcheck, PSScriptAnalyzer...)
    max_line: report lines longer than this (0 = never)
    """
    return lint_paths(workspace, path, languages, external=external, max_line=max_line, max_findings=max_findings,
                      min_severity=min_severity)


@action("lint.fix", writes=True, needs=["ruff", "eslint"])
def fix(workspace: Path, path: str = ".", languages: Optional[list[str]] = None) -> dict:
    """Apply the fixes the installed linters can make safely (ruff --fix, eslint --fix), then report what is left

    path: a file or folder inside the working folder
    languages: only these
    """
    return lint_paths(workspace, path, languages, fix=True)


@action("lint.snippet")
def snippet(code: str, language: str) -> dict:
    """Check a piece of code without saving it: its syntax and the built-in checks for that language

    code: the source text
    language: python, json, yaml, toml, xml, html, css, powershell, batch, vbscript, sql, javascript, shell...
    """
    import tempfile
    ext = {lang: exts[0] for lang, exts in __import__("abp_toolkit.util", fromlist=["LANGUAGES"]).LANGUAGES.items()}
    if language not in ext:
        raise ToolkitError(f"unknown language {language!r}; one of {', '.join(sorted(ext))}")
    with tempfile.TemporaryDirectory() as d:
        name = "Dockerfile" if language == "dockerfile" else "snippet" + ext[language]
        (Path(d) / name).write_text(code, encoding="utf-8")
        result = lint_paths(Path(d), name, max_line=0)
    return result


@action("lint.languages")
def languages() -> dict:
    """Which languages the linter understands, and which external linters are installed on this machine"""
    from abp_toolkit.util import LANGUAGES
    tools = {"ruff", "mypy", "eslint", "tsc", "node", "shellcheck", "bash", "pwsh", "powershell", "go", "gofmt",
             "cargo", "yamllint", "hadolint", "rubocop", "php", "luacheck", "cppcheck", "stylelint", "markdownlint", "sqlfluff"}
    return {"languages": {lang: {"extensions": list(exts), "builtin": lang in BUILTIN,
                                 "external": [getattr(f, "__name__", "").replace("ext_", "") for f in EXTERNAL.get(lang, [])]}
                          for lang, exts in LANGUAGES.items()},
            "installed": sorted(t for t in tools if which(t)), "not_installed": {t: INSTALL_HINTS.get(t, "") for t in sorted(tools) if not which(t)},
            "python": sys.version.split()[0]}
