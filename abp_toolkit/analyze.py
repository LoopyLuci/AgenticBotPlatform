"""Understanding code: size and languages, complexity, imports, duplicates, unused code, risky patterns, notes.

Python is analyzed through its syntax tree (exact). Other languages get careful text analysis: comments and strings
are recognized per language family, complexity counts decision points, imports are read from each language's own
import syntax.
"""
from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, is_binary, language_of, read_text, rel, walk

group("analyze", "Understand code: metrics, complexity, imports, duplicates, unused code, risky patterns, TODOs")

COMMENT = {
    "hash": (re.compile(r"#.*$"), None),
    "c": (re.compile(r"//.*$"), (re.compile(r"/\*"), re.compile(r"\*/"))),
    "sql": (re.compile(r"--.*$"), (re.compile(r"/\*"), re.compile(r"\*/"))),
    "vb": (re.compile(r"'.*$|^\s*rem\b.*$", re.I), None),
    "bat": (re.compile(r"^\s*(?:rem\b|::).*$", re.I), None),
    "ps": (re.compile(r"#.*$"), (re.compile(r"<#"), re.compile(r"#>"))),
    "html": (None, (re.compile(r"<!--"), re.compile(r"-->"))),
    "lua": (re.compile(r"--.*$"), (re.compile(r"--\[\["), re.compile(r"\]\]"))),
}
FAMILY = {"python": "hash", "shell": "hash", "ruby": "hash", "perl": "hash", "r": "hash", "yaml": "hash", "toml": "hash",
          "dockerfile": "hash", "ini": "hash", "powershell": "ps", "sql": "sql", "vbscript": "vb", "batch": "bat",
          "html": "html", "xml": "html", "markdown": "html", "lua": "lua"}
DECISIONS = re.compile(r"\b(if|elif|else\s+if|elseif|for|foreach|while|case|when|catch|except|and|or|until|unless)\b|&&|\|\||\?(?!\?)|\bElseIf\b", re.I)


def line_kinds(text: str, lang: Optional[str]) -> tuple[int, int, int]:
    """(code, comment, blank) line counts."""
    single, block = COMMENT.get(FAMILY.get(lang or "", "c"), COMMENT["c"])
    code = comment = blank = 0
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            blank += 1
            continue
        if in_block:
            comment += 1
            if block and block[1].search(s):
                in_block = False
            continue
        if block and block[0].match(s):
            comment += 1
            in_block = not block[1].search(s[len(block[0].match(s).group(0)):])
            continue
        if single and single.match(s):
            comment += 1
            continue
        if lang == "python" and s.startswith(('"""', "'''")) and (s.count('"""') + s.count("'''")) >= 2 and len(s) > 5:
            comment += 1
            continue
        code += 1
    return code, comment, blank


@action("analyze.overview")
def overview(workspace: Path, path: str = ".", top: int = 15) -> dict:
    """A codebase at a glance: files and lines per language (code, comments, blank), biggest files, folders

    path: a file or folder inside the working folder
    top: how many of the biggest files to list
    """
    target = inside(workspace, path, must_exist=True)
    per: dict[str, Counter] = defaultdict(Counter)
    sizes = []
    folders: Counter = Counter()
    for f in walk(target):
        if is_binary(f):
            continue
        lang = language_of(f) or "other"
        text = read_text(f)
        c, cm, b = line_kinds(text, lang)
        per[lang].update(files=1, code=c, comments=cm, blank=b, bytes=len(text.encode("utf-8", "ignore")))
        sizes.append((c, rel(workspace, f)))
        folders[rel(workspace, f).split("/")[0] if "/" in rel(workspace, f) else "."] += c
    total = sum((v for v in per.values()), Counter())
    return {"total": dict(total), "languages": {k: dict(v) for k, v in sorted(per.items(), key=lambda kv: -kv[1]["code"])},
            "largest_files": [{"file": f, "code_lines": c} for c, f in sorted(sizes, reverse=True)[:top]],
            "top_folders": dict(folders.most_common(top))}


# ---- complexity ---------------------------------------------------------------------------------------------------------
class _PyComplexity(ast.NodeVisitor):
    def __init__(self) -> None:
        self.funcs: list[dict] = []
        self.stack: list[str] = []

    def _func(self, node) -> None:
        score = 1
        for n in ast.walk(node):
            if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.With,
                              ast.AsyncWith, ast.Assert, ast.comprehension)):
                score += 1
            elif isinstance(n, ast.BoolOp):
                score += len(n.values) - 1
            elif isinstance(n, ast.match_case) if hasattr(ast, "match_case") else False:
                score += 1
        depth = _max_depth(node)
        name = ".".join(self.stack + [node.name])
        self.funcs.append({"name": name, "line": node.lineno, "complexity": score, "lines": (node.end_lineno or node.lineno) - node.lineno + 1,
                           "params": len(node.args.args) + len(node.args.kwonlyargs), "nesting": depth})
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _max_depth(node: ast.AST, depth: int = 0) -> int:
    best = depth
    for child in ast.iter_child_nodes(node):
        nested = isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith))
        best = max(best, _max_depth(child, depth + 1 if nested else depth))
    return best


FUNC_START = {
    "c": re.compile(r"^\s*(?:(?:public|private|protected|internal|static|async|export|override|virtual|final|fn|func|function|def|sub|let|const|var)\s+)*"
                    r"(?:[\w<>\[\],\s*&:]+\s+)?(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?:async\s*)?\([^)]*\)\s*=>|\([^;{]*\)\s*(?:->\s*[\w<>:&\s]+)?\s*(?:throws [\w, ]+)?\{?\s*$)"),
    "ps": re.compile(r"^\s*function\s+(?P<name>[\w-]+)", re.I),
    "vb": re.compile(r"^\s*(?:(?:public|private)\s+)?(?:sub|function)\s+(?P<name>\w+)", re.I),
    "bat": re.compile(r"^:(?P<name>\w+)"),
    "hash": re.compile(r"^\s*(?:def|function)\s+(?P<name>[\w-]+)|^(?P<name2>[\w-]+)\s*\(\)\s*\{"),
}


def _text_functions(text: str, lang: str) -> list[dict]:
    fam = FAMILY.get(lang, "c")
    pat = FUNC_START.get(fam if fam in FUNC_START else "c")
    lines = text.splitlines()
    starts = []
    for i, l in enumerate(lines):
        m = pat.match(l)
        if m and (m.group("name") if "name" in m.groupdict() and m.group("name") else m.groupdict().get("name2")) \
                and not re.match(r"^\s*(if|for|while|switch|catch|return|else)\b", l):
            starts.append((i, m.group("name") or m.groupdict().get("name2")))
    out = []
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = "\n".join(lines[i:end])
        out.append({"name": name, "line": i + 1, "complexity": 1 + len(DECISIONS.findall(body)), "lines": end - i})
    return out


@action("analyze.complexity")
def complexity(workspace: Path, path: str = ".", threshold: int = 10, limit: int = 50) -> dict:
    """The most complex functions (cyclomatic complexity), with their length, parameters and nesting

    path: a file or folder inside the working folder
    threshold: report functions at or above this complexity
    limit: how many to list
    """
    target = inside(workspace, path, must_exist=True)
    found = []
    total_funcs = 0
    for f in walk(target):
        lang = language_of(f)
        if not lang or is_binary(f) or lang in ("json", "yaml", "toml", "xml", "markdown", "csv", "ini", "html", "css"):
            continue
        text = read_text(f)
        if lang == "python":
            try:
                v = _PyComplexity()
                v.visit(ast.parse(text))
                funcs = v.funcs
            except SyntaxError:
                continue
        else:
            funcs = _text_functions(text, lang)
        total_funcs += len(funcs)
        found.extend({"file": rel(workspace, f), "language": lang, **fn} for fn in funcs)
    found.sort(key=lambda x: -x["complexity"])
    over = [x for x in found if x["complexity"] >= threshold]
    scores = [x["complexity"] for x in found] or [0]
    return {"functions": total_funcs, "average": round(sum(scores) / max(1, len(found)), 2), "max": max(scores),
            "over_threshold": len(over), "threshold": threshold, "worst": over[:limit],
            "grades": {g: sum(1 for s in scores if lo <= s <= hi) for g, lo, hi in
                       (("A 1-5", 1, 5), ("B 6-10", 6, 10), ("C 11-20", 11, 20), ("D 21-30", 21, 30), ("F 31+", 31, 10**9))}}


# ---- imports ------------------------------------------------------------------------------------------------------------
IMPORT_RES = {
    "javascript": re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"]([^'"]+)['"]"""),
    "typescript": re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"]([^'"]+)['"]"""),
    "go": re.compile(r'^\s*(?:import\s+)?"([^"]+)"', re.M),
    "rust": re.compile(r"^\s*(?:use|extern crate)\s+([\w:]+)", re.M),
    "java": re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M),
    "kotlin": re.compile(r"^\s*import\s+([\w.]+)", re.M),
    "csharp": re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;", re.M),
    "c": re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M),
    "cpp": re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M),
    "ruby": re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", re.M),
    "php": re.compile(r"""^\s*(?:use\s+([\w\\]+)|(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)['"])""", re.M),
    "powershell": re.compile(r"""^\s*(?:Import-Module\s+([\w.\-\\/]+)|\.\s+['"]?([^'"\s]+\.ps1))""", re.M | re.I),
    "shell": re.compile(r"""^\s*(?:source|\.)\s+['"]?([^'"\s]+)""", re.M),
    "batch": re.compile(r"""^\s*call\s+['"]?([^'"\s:]+\.(?:bat|cmd))""", re.M | re.I),
    "lua": re.compile(r"""require\s*\(?\s*['"]([^'"]+)['"]"""),
    "dart": re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.M),
    "css": re.compile(r"""@import\s+(?:url\()?['"]?([^'")\s;]+)"""),
}


def _python_imports(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = ("." * n.level) + (n.module or "")
            out.append(base)
            # `from pkg import b` may import the submodule pkg.b; imports() keeps it only if such a module exists.
            out.extend(f"{base}.{a.name}" for a in n.names if a.name != "*" and not base.endswith("."))
    return out


@action("analyze.imports")
def imports(workspace: Path, path: str = ".", internal_only: bool = False, limit: int = 400) -> dict:
    """What each file imports, the external packages used most, internal modules nothing imports, and import cycles

    path: a file or folder inside the working folder
    internal_only: leave out third-party and standard-library imports
    limit: how many edges to return
    """
    target = inside(workspace, path, must_exist=True)
    root = Path(workspace).resolve()
    edges: list[tuple[str, str]] = []
    external: Counter = Counter()
    modules: dict[str, str] = {}
    for f in walk(target):
        lang = language_of(f)
        if lang == "python":
            modname = rel(root, f)[:-3].replace("/", ".").removesuffix(".__init__")
            modules[modname] = rel(root, f)
    for f in walk(target):
        lang = language_of(f)
        if not lang or is_binary(f):
            continue
        text = read_text(f)
        if lang == "python":
            found = _python_imports(text)
        elif lang in IMPORT_RES:
            found = [next(g for g in m.groups() if g) for m in IMPORT_RES[lang].finditer(text) if any(m.groups())]
        else:
            continue
        me = rel(root, f)
        if lang == "python":
            # drop `pkg.name` guesses where pkg is a module but pkg.name is not (it was a function or class)
            found = [i for i in found if not (i.count(".") and i.rsplit(".", 1)[0] in modules and i not in modules)]
        for imp in found:
            internal = imp.startswith(".") or any(imp == m or imp.startswith(m + ".") for m in modules) or \
                (lang not in ("python", "java", "kotlin", "csharp", "rust", "go") and (imp.startswith("./") or imp.startswith("../")))
            if internal:
                edges.append((me, imp))
            else:
                external[imp.split("/")[0] if lang in ("javascript", "typescript") and not imp.startswith("@") else
                         "/".join(imp.split("/")[:2]) if imp.startswith("@") else imp.split(".")[0].split("::")[0]] += 1
                if not internal_only:
                    edges.append((me, imp))
    imported = {i for _f, i in edges}
    unused = sorted(m for m in modules if not any(i == m or i.endswith("." + m.split(".")[-1]) and m.endswith(i.lstrip(".")) for i in imported)
                    and not m.endswith(("__main__", "conftest")) and not m.split(".")[-1].startswith("test"))
    # cycles among Python modules (absolute imports only)
    graph: dict[str, set[str]] = defaultdict(set)
    for f, i in edges:
        src = f[:-3].replace("/", ".").removesuffix(".__init__") if f.endswith(".py") else None
        if src and i in modules:
            graph[src].add(i)
    cycles = _cycles(graph)
    return {"edges": [{"from": a, "imports": b} for a, b in edges[:limit]], "edge_count": len(edges),
            "external_packages": dict(external.most_common(60)), "modules_nothing_imports": unused[:100],
            "cycles": cycles[:20]}


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))
    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    for v in list(graph):
        if v not in index:
            strong(v)
    return out


# ---- duplicates ---------------------------------------------------------------------------------------------------------
@action("analyze.duplicates")
def duplicates(workspace: Path, path: str = ".", min_lines: int = 8, limit: int = 30) -> dict:
    """Blocks of code copied in more than one place (whitespace and comments ignored)

    path: a file or folder inside the working folder
    min_lines: the shortest block worth reporting
    limit: how many to list
    """
    target = inside(workspace, path, must_exist=True)
    windows: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for f in walk(target):
        lang = language_of(f)
        if not lang or is_binary(f) or lang in ("json", "csv", "markdown", "xml", "yaml"):
            continue
        norm = []
        for i, line in enumerate(read_text(f).splitlines(), 1):
            s = re.sub(r"\s+", " ", line.strip())
            if s and not re.match(r"^(#|//|--|'|/\*|\*|rem\b|::)", s, re.I) and s not in ("{", "}", ")", "];", "end", "fi", "done"):
                norm.append((i, s))
        for k in range(len(norm) - min_lines + 1):
            block = "\n".join(s for _, s in norm[k:k + min_lines])
            windows[hashlib.sha1(block.encode()).hexdigest()].append((rel(workspace, f), norm[k][0]))
    groups = []
    seen: set[tuple[str, int]] = set()
    for places in windows.values():
        uniq = sorted(set(places))
        if len(uniq) < 2 or all((p, l - 1) in seen or (p, l) in seen for p, l in uniq):
            seen.update(uniq)
            continue
        seen.update(uniq)
        groups.append({"lines": min_lines, "places": [{"file": p, "line": l} for p, l in uniq[:10]], "copies": len(uniq)})
    groups.sort(key=lambda g: -g["copies"])
    return {"blocks": len(groups), "duplicates": groups[:limit]}


# ---- unused Python code -------------------------------------------------------------------------------------------------
@action("analyze.unused")
def unused(workspace: Path, path: str = ".", limit: int = 100) -> dict:
    """Python functions and classes defined but never referenced anywhere in the folder (likely dead code)

    path: a folder (or file) inside the working folder
    limit: how many to list
    """
    target = inside(workspace, path, must_exist=True)
    defs: list[tuple[str, str, int, str]] = []
    names: Counter = Counter()
    texts = []
    for f in walk(target, extensions={".py"}):
        text = read_text(f)
        texts.append(text)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # A decorated function is registered somewhere (a route, an action, a fixture), and visit_* /
                # handle_* / do_* methods are called by name by their framework: none of them is dead.
                if n.decorator_list or re.match(r"(visit|generic_visit|handle|do|on|test)_", n.name):
                    continue
                defs.append((n.name, rel(workspace, f), n.lineno, "class" if isinstance(n, ast.ClassDef) else "function"))
            elif isinstance(n, ast.Name):
                names[n.id] += 1
            elif isinstance(n, ast.Attribute):
                names[n.attr] += 1
    joined = "\n".join(texts)
    out = []
    for name, f, line, kind in defs:
        if name.startswith("__") or name.startswith("test") or name in ("main", "setUp", "tearDown") or names[name]:
            continue
        if re.search(r"['\"]" + re.escape(name) + r"['\"]", joined):     # referenced by name (getattr, registries)
            continue
        if f.split("/")[-1].startswith("test_"):
            continue
        out.append({"name": name, "kind": kind, "file": f, "line": line})
    return {"candidates": len(out), "unused": out[:limit],
            "note": "decorated handlers, plugin hooks and public APIs used from outside this folder appear here too"}


# ---- risky patterns -----------------------------------------------------------------------------------------------------
RISKS = [
    ("python", "eval-exec", "high", re.compile(r"\b(eval|exec)\s*\("), "runs text as code"),
    ("python", "shell-true", "high", re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "a shell parses the command: injection risk"),
    ("python", "os-system", "medium", re.compile(r"\bos\.(system|popen)\s*\("), "runs a command through the shell"),
    ("python", "pickle-load", "high", re.compile(r"\bpickle\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader)"), "loading untrusted data can run code"),
    ("python", "sql-format", "high", re.compile(r"""(?i)execute\s*\(\s*f?["'].*(select|insert|update|delete)\b.*(\{|%s?|\+\s*\w)"""), "SQL built from strings: injection risk (use parameters)"),
    ("python", "verify-false", "high", re.compile(r"verify\s*=\s*False"), "TLS certificate checking is turned off"),
    ("python", "md5-sha1", "low", re.compile(r"hashlib\.(md5|sha1)\s*\("), "weak hash (fine for checksums, not for passwords or signatures)"),
    ("python", "tempfile-mktemp", "medium", re.compile(r"tempfile\.mktemp\s*\("), "race-prone temporary file name"),
    ("python", "assert-security", "low", re.compile(r"^\s*assert\s+.*(auth|perm|admin|user)", re.I), "asserts vanish under python -O"),
    ("javascript", "eval", "high", re.compile(r"\beval\s*\(|new\s+Function\s*\("), "runs text as code"),
    ("javascript", "innerhtml", "medium", re.compile(r"\.innerHTML\s*=(?!\s*['\"`]\s*['\"`])"), "HTML from data: XSS risk unless escaped"),
    ("javascript", "child-exec", "high", re.compile(r"child_process\.exec\s*\(|\bexecSync\s*\("), "runs a command through a shell"),
    ("typescript", "eval", "high", re.compile(r"\beval\s*\(|new\s+Function\s*\("), "runs text as code"),
    ("typescript", "innerhtml", "medium", re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML"), "HTML from data: XSS risk unless escaped"),
    ("powershell", "invoke-expression", "high", re.compile(r"(?i)\b(Invoke-Expression|iex)\b"), "runs text as code"),
    ("powershell", "download-exec", "high", re.compile(r"(?i)(DownloadString|Invoke-WebRequest|iwr|irm)[^\n|]*\|\s*(iex|Invoke-Expression)"), "downloads and runs code"),
    ("powershell", "bypass-policy", "low", re.compile(r"(?i)-ExecutionPolicy\s+Bypass"), "execution policy bypassed"),
    ("batch", "del-quiet", "medium", re.compile(r"(?i)\b(del|erase)\s+/[sq]\b.*|\brd\s+/s\s+/q\b"), "deletes without asking"),
    ("vbscript", "execute", "high", re.compile(r"(?i)\b(Execute|ExecuteGlobal|Eval)\s*\("), "runs text as code"),
    ("vbscript", "wscript-run", "medium", re.compile(r"(?i)\.Run\s*\(|\.Exec\s*\("), "starts other programs"),
    ("shell", "curl-sh", "high", re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba)?sh"), "downloads and runs code"),
    ("shell", "rm-rf-var", "high", re.compile(r"rm\s+-rf\s+\"?\$\w*\"?/?\s*$|rm\s+-rf\s+/\s"), "can delete everything if the variable is empty"),
    ("shell", "chmod-777", "medium", re.compile(r"chmod\s+(-R\s+)?777"), "everyone can write it"),
    ("php", "eval", "high", re.compile(r"\b(eval|assert|system|shell_exec|passthru)\s*\("), "runs text as code or commands"),
    ("sql", "grant-all", "medium", re.compile(r"(?i)grant\s+all\b"), "grants every privilege"),
    ("dockerfile", "root-user", "low", re.compile(r"(?i)^\s*user\s+root\s*$"), "the container runs as root"),
]


@action("analyze.risks")
def risks(workspace: Path, path: str = ".", min_level: str = "low", limit: int = 200) -> dict:
    """Risky code: running text as code, shell injection, SQL built from strings, TLS checks off, download-and-run, secrets

    path: a file or folder inside the working folder
    min_level: low, medium or high
    limit: how many to list
    """
    from abp_toolkit.lint import SECRET_PATTERNS
    order = {"low": 0, "medium": 1, "high": 2}
    target = inside(workspace, path, must_exist=True)
    found = []
    for f in walk(target):
        lang = language_of(f)
        if is_binary(f):
            continue
        text = read_text(f)
        for i, line in enumerate(text.splitlines(), 1):
            for rlang, code, level, pat, why in RISKS:
                if rlang == lang and order[level] >= order.get(min_level, 0) and pat.search(line):
                    found.append({"file": rel(workspace, f), "line": i, "level": level, "rule": code, "why": why,
                                  "code": line.strip()[:160]})
            for code, pat in SECRET_PATTERNS:
                if pat.search(line) and "example" not in line.lower():
                    found.append({"file": rel(workspace, f), "line": i, "level": "high", "rule": f"secret/{code}",
                                  "why": "a secret in the source", "code": "(hidden)"})
    found.sort(key=lambda x: (-order[x["level"]], x["file"], x["line"]))
    return {"count": len(found), "by_level": dict(Counter(x["level"] for x in found)), "findings": found[:limit]}


@action("analyze.todos")
def todos(workspace: Path, path: str = ".", tags: Optional[list[str]] = None, limit: int = 300) -> dict:
    """TODO, FIXME, HACK, XXX and BUG notes left in the code, with where they are

    path: a file or folder inside the working folder
    tags: which markers to look for
    """
    tags = tags or ["TODO", "FIXME", "HACK", "XXX", "BUG", "OPTIMIZE"]
    pat = re.compile(r"\b(" + "|".join(map(re.escape, tags)) + r")\b[:(]?\s*(.*)")
    target = inside(workspace, path, must_exist=True)
    out = []
    for f in walk(target):
        if is_binary(f) or not language_of(f):
            continue
        for i, line in enumerate(read_text(f).splitlines(), 1):
            m = pat.search(line)
            if m and re.search(r"(#|//|--|'|/\*|<!--|\*|rem\b|::)", line[:m.start()] or line, re.I):
                out.append({"file": rel(workspace, f), "line": i, "tag": m.group(1), "text": m.group(2).strip()[:200]})
    return {"count": len(out), "by_tag": dict(Counter(x["tag"] for x in out)), "notes": out[:limit]}


@action("analyze.symbols")
def symbols(workspace: Path, path: str, include_private: bool = False) -> dict:
    """The outline of one file: classes, functions and methods with their lines and signatures

    path: a source file inside the working folder
    include_private: include names starting with _
    """
    f = inside(workspace, path, must_exist=True)
    lang = language_of(f)
    text = read_text(f)
    if lang == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise ToolkitError(f"syntax error on line {e.lineno}: {e.msg}") from e
        out = []

        def walk_nodes(nodes, prefix=""):
            for n in nodes:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if n.name.startswith("_") and not n.name.startswith("__") and not include_private:
                        continue
                    kind = "class" if isinstance(n, ast.ClassDef) else "method" if prefix else "function"
                    sig = ""
                    if not isinstance(n, ast.ClassDef):
                        sig = "(" + ", ".join(a.arg for a in n.args.args) + ")"
                    out.append({"name": prefix + n.name, "kind": kind, "line": n.lineno, "end": n.end_lineno,
                                "signature": sig, "doc": (ast.get_docstring(n) or "").split("\n")[0][:120]})
                    if isinstance(n, ast.ClassDef):
                        walk_nodes(n.body, prefix + n.name + ".")
        walk_nodes(tree.body)
        return {"file": rel(workspace, f), "language": lang, "symbols": out}
    return {"file": rel(workspace, f), "language": lang,
            "symbols": [{"name": x["name"], "kind": "function", "line": x["line"]} for x in _text_functions(text, lang or "")]}
