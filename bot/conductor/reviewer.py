"""The reviewer: a diff against the owner's standing rules, before any merge.

Static checks first (pure, no model, same answer every time), each producing findings with the exact file and line:

    no_feature_removed  the hardest rule. A removed route, CLI command or flag, MCP tool, dashboard section or page, config key,
                        public function or class still named in the docs, or enum variant is a rejection - unless the same thing
                        is added back in the diff (a move or a rename keeps it).
    no_deletions        a deleted file the task did not create (and was not told it may delete)
    leak_guard          machine paths, user names, private project names in what a public repo gains (scripts/leak_guard.py)
    hotreload           a new bot/ module that bot/hotreload.py does not classify
    tests               code changed and no test added or changed
    ruff                ruff on the touched Python files, as they are on the branch

Then, if asked, a local model reviews the diff (capped prompt, a separate call from the planner's); its concerns are advisory unless
it rejects. The report is a dataclass with `to_dict()` (machine) and `to_text()` (human). Nothing here modifies the repository.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

ACCEPT, REJECT = "accept", "reject"
RULES = ("no_feature_removed", "no_deletions", "leak_guard", "hotreload", "tests", "ruff", "model")
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".kt", ".java", ".c", ".cpp", ".cs", ".html"}
WEB_EXT = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue"}
CONFIG_EXT = {".yaml", ".yml"}


@dataclass
class Finding:
    rule: str
    message: str
    file: str = ""
    line: int = 0
    kind: str = ""
    token: str = ""
    text: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0)}

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}: " if self.file and self.line else (f"{self.file}: " if self.file else "")
        return f"[{self.rule}] {where}{self.message}" + (f"\n      {self.text.strip()[:200]}" if self.text else "")


@dataclass
class Review:
    verdict: str = ACCEPT
    findings: list[Finding] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)       # rule -> "pass" | "fail" | "skipped: why"
    advisory: list[Finding] = field(default_factory=list)      # leak-guard warnings, model concerns: noted, not blocking
    base: str = ""
    head: str = ""
    files: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict == ACCEPT

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "base": self.base, "head": self.head, "files": self.files, "model": self.model,
                "checks": self.checks, "findings": [f.to_dict() for f in self.findings],
                "advisory": [f.to_dict() for f in self.advisory]}

    def to_text(self) -> str:
        lines = [f"review: {self.verdict.upper()}  ({self.base[:12]}..{self.head[:12]}, {len(self.files)} file(s))"]
        lines += [f"  {rule}: {state}" for rule, state in self.checks.items()]
        if self.findings:
            lines.append("reasons:")
            lines += [f"  - {f}" for f in self.findings]
        if self.advisory:
            lines.append("notes:")
            lines += [f"  - {f}" for f in self.advisory]
        return "\n".join(lines)

    def feedback(self) -> str:
        """What a worker is told to fix: the reasons, one per line."""
        return "\n".join(str(f) for f in self.findings)


# ------------------------------------------------------------------- git
def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _show(repo: Path, ref: str, rel: str) -> Optional[str]:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"], capture_output=True, timeout=60)
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None


@dataclass
class Change:
    status: str            # A M D R
    path: str              # the path at head (the old path for D)
    old: str = ""          # the old path of a rename


def changes(repo: Path, base: str, head: str) -> list[Change]:
    out = []
    for ln in _git(repo, "diff", "--name-status", "-M", "--no-color", base, head).splitlines():
        parts = ln.split("\t")
        if not parts or not parts[0]:
            continue
        st = parts[0][0]
        if st == "R" and len(parts) >= 3:
            out.append(Change("R", parts[2], parts[1]))
        elif len(parts) >= 2:
            out.append(Change(st, parts[-1]))
    return out


@dataclass
class DiffLine:
    file: str
    line: int
    text: str


def parse_diff(diff: str) -> tuple[list[DiffLine], list[DiffLine]]:
    """(removed lines with their line number in the old file, added lines with theirs in the new)."""
    removed: list[DiffLine] = []
    added: list[DiffLine] = []
    old_file = new_file = ""
    old_n = new_n = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            continue
        if not in_hunk:
            if raw.startswith("--- "):
                old_file = raw[4:].strip()
                old_file = old_file[2:] if old_file.startswith("a/") else old_file
                continue
            if raw.startswith("+++ "):
                new_file = raw[4:].strip()
                new_file = new_file[2:] if new_file.startswith("b/") else new_file
                continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            old_n, new_n, in_hunk = int(m.group(1)), int(m.group(2)), True
            continue
        if not in_hunk:
            continue
        if raw.startswith("-"):
            removed.append(DiffLine(old_file if old_file != "/dev/null" else new_file, old_n, raw[1:]))
            old_n += 1
        elif raw.startswith("+"):
            added.append(DiffLine(new_file if new_file != "/dev/null" else old_file, new_n, raw[1:]))
            new_n += 1
    return removed, added


# --------------------------------------------------------- feature tokens
_ROUTE = re.compile(r"""@\s*[\w.]+\.(get|post|put|delete|patch|head|options|websocket|route|api_route)\(\s*[rf]?["']([^"']+)["']""")
_CLI_CMD = re.compile(r"""add_parser\(\s*["']([\w][\w-]*)["']""")
_QUOTED = re.compile(r"""["']([^"'\s]+)["']""")
_MCP_TOOL = re.compile(r"""^\s*@\s*[\w.]*\btool\s*\(""")
_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(")
_CLASS = re.compile(r"^(\s*)class\s+([A-Za-z]\w*)")
_WEB_ATTR = re.compile(r"""\bdata-(tab|page|section|view|panel)\s*=\s*["']([^"']+)["']""")
_WEB_SECTION = re.compile(r"""<section\b[^>]*\bid\s*=\s*["']([^"']+)["']""")
_WEB_ROOT = re.compile(r"""\bid\s*=\s*["']([\w-]+-(?:root|panel|page|section))["']""")
_WEB_HEAD = re.compile(r"""<h[12]\b[^>]*>\s*([^<]{3,60}?)\s*</h[12]>""")
_YAML_KEY = re.compile(r"^(\s*)([A-Za-z_][\w.\-]*)\s*:(?:\s|$)")


def _flags(line: str) -> list[str]:
    if "add_argument" not in line and not re.search(r"\b(?:click\.)?option\(", line):
        return []
    return [q for q in _QUOTED.findall(line) if q.startswith("-") and len(q) > 1 and re.fullmatch(r"-{1,2}[\w][\w-]*", q)]


def _positional(line: str) -> list[str]:
    if "add_argument" not in line:
        return []
    qs = _QUOTED.findall(line.split("add_argument", 1)[1])
    return [qs[0]] if qs and not qs[0].startswith("-") and re.fullmatch(r"[\w][\w-]*", qs[0]) else []


def features(path: str, text: str, pending_tool: bool = False) -> tuple[list[tuple[str, str]], bool]:
    """The (kind, token) features one line of `path` declares, and whether an MCP tool decorator is waiting for its def."""
    ext = Path(path).suffix.lower()
    found: list[tuple[str, str]] = []
    if ext == ".py":
        for m in _ROUTE.finditer(text):
            found.append(("route", f"{m.group(1).upper()} {m.group(2)}"))
        for m in _CLI_CMD.finditer(text):
            found.append(("CLI command", m.group(1)))
        found += [("CLI flag", f) for f in _flags(text)]
        found += [("CLI argument", p) for p in _positional(text)]
        if _MCP_TOOL.match(text):
            return found, True
        m = _DEF.match(text)
        if m and pending_tool:
            found.append(("MCP tool", m.group(2)))
        return found, False if m else pending_tool
    if ext in WEB_EXT:
        found += [("dashboard section", f"{m.group(1)}={m.group(2)}") for m in _WEB_ATTR.finditer(text)]
        found += [("dashboard section", m.group(1)) for m in _WEB_SECTION.finditer(text)]
        found += [("dashboard section", m.group(1)) for m in _WEB_ROOT.finditer(text)]
        if ext in (".html", ".htm"):
            found += [("dashboard page heading", m.group(1)) for m in _WEB_HEAD.finditer(text)]
    return found, pending_tool


def _skip_features(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith(("tests/", "docs/", "examples/")) or "/tests/" in p or p.endswith((".md", ".txt", ".lock")) \
        or Path(p).name.startswith("test_")


def _doc_text(repo: Path, ref: str) -> str:
    try:
        names = _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
    except RuntimeError:
        return ""
    docs = [n for n in names if n.lower().endswith(".md") and (n.startswith("docs/") or "/" not in n)]
    return "\n".join(_show(repo, ref, n) or "" for n in docs[:400])


def _doc_mentions(docs: str, name: str) -> bool:
    return re.search(r"`[^`\n]*\b" + re.escape(name) + r"\b[^`\n]*`", docs) is not None


def check_features(repo: Path, base: str, head: str, removed: list[DiffLine], added: list[DiffLine],
                   file_changes: list[Change]) -> list[Finding]:
    renamed = {c.old: c.path for c in file_changes if c.status == "R"}
    kept: set[tuple[str, str]] = set()
    kept_names: set[str] = set()
    kept_keys: set[tuple[str, str]] = set()
    pending: dict[str, bool] = {}
    for dl in added:
        if _skip_features(dl.file):
            continue
        feats, pending[dl.file] = features(dl.file, dl.text, pending.get(dl.file, False))
        kept.update(feats)
        for pat in (_DEF, _CLASS):
            m = pat.match(dl.text)
            if m:
                kept_names.add(m.group(2))
        y = _YAML_KEY.match(dl.text) if Path(dl.file).suffix.lower() in CONFIG_EXT else None
        if y:
            kept_keys.add((dl.file, y.group(2)))
    findings: list[Finding] = []
    docs: Optional[str] = None
    pending = {}
    seen: set[tuple[str, str, str]] = set()
    for dl in removed:
        if _skip_features(dl.file):
            continue
        new_path = renamed.get(dl.file, dl.file)
        feats, pending[dl.file] = features(dl.file, dl.text, pending.get(dl.file, False))
        for kind, token in feats:
            if (kind, token) in kept or (kind, token, dl.file) in seen:
                continue
            seen.add((kind, token, dl.file))
            if kind == "MCP tool" and token in kept_names:
                continue
            findings.append(Finding("no_feature_removed", f"{kind} removed: {token}", dl.file, dl.line, kind, token, dl.text))
        if dl.file.endswith(".py"):
            m = _DEF.match(dl.text) or _CLASS.match(dl.text)
            if m and not m.group(2).startswith("_") and m.group(2) not in kept_names and (m.group(2), "def", dl.file) not in {
                    (f.token, "def", f.file) for f in findings}:
                if docs is None:
                    docs = _doc_text(repo, head)
                if _doc_mentions(docs, m.group(2)):
                    what = "class" if _CLASS.match(dl.text) else "function"
                    findings.append(Finding("no_feature_removed", f"public {what} removed but still named in the docs: {m.group(2)}",
                                            dl.file, dl.line, f"public {what}", m.group(2), dl.text))
        if Path(new_path).suffix.lower() in CONFIG_EXT and (new_path.startswith("config/") or "/config/" in new_path):
            y = _YAML_KEY.match(dl.text)
            if y and (new_path, y.group(2)) not in kept_keys and (dl.file, y.group(2)) not in kept_keys:
                findings.append(Finding("no_feature_removed", f"config key removed: {y.group(2)}", dl.file, dl.line, "config key",
                                        y.group(2), dl.text))
    findings += _enum_variants(repo, base, head, file_changes)
    return findings


def _enum_members(src: str) -> dict[str, dict[str, int]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, dict[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
                (b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")).endswith(("Enum", "Flag")) for b in node.bases):
            members: dict[str, int] = {}
            for st in node.body:
                if isinstance(st, ast.Assign):
                    for t in st.targets:
                        if isinstance(t, ast.Name) and not t.id.startswith("_"):
                            members[t.id] = st.lineno
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    members[st.target.id] = st.lineno
            out[node.name] = members
    return out


def _enum_variants(repo: Path, base: str, head: str, file_changes: list[Change]) -> list[Finding]:
    findings = []
    for c in file_changes:
        if c.status not in ("M", "R") or not c.path.endswith(".py") or _skip_features(c.path):
            continue
        old_src, new_src = _show(repo, base, c.old or c.path), _show(repo, head, c.path)
        if old_src is None or new_src is None:
            continue
        old_enums, new_enums = _enum_members(old_src), _enum_members(new_src)
        old_lines = old_src.splitlines()
        for cls, members in old_enums.items():
            if cls not in new_enums:
                continue                                 # the class itself went: the public-class check names it if documented
            for name, lineno in members.items():
                if name not in new_enums[cls]:
                    text = old_lines[lineno - 1] if lineno - 1 < len(old_lines) else ""
                    findings.append(Finding("no_feature_removed", f"enum variant removed: {cls}.{name}", c.old or c.path, lineno,
                                            "enum variant", f"{cls}.{name}", text))
    return findings


# ---------------------------------------------------------- other checks
def check_deletions(file_changes: list[Change], created: Iterable[str], allow: Iterable[str]) -> list[Finding]:
    ok = {p.replace("\\", "/") for p in (*created, *allow)}
    return [Finding("no_deletions", "file deleted, and the task did not create it", c.path, 0, "deleted file", c.path)
            for c in file_changes if c.status == "D" and c.path.replace("\\", "/") not in ok]


def _leak_module(root: Optional[Path] = None):
    path = (root or Path(__file__).resolve().parents[2]) / "scripts" / "leak_guard.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("conductor_leak_guard", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("conductor_leak_guard", mod)
    spec.loader.exec_module(mod)
    return mod


def check_leaks(added: list[DiffLine], private_dir: Optional[Path]) -> tuple[list[Finding], list[Finding], str]:
    """(blocking findings, advisory findings, check state) over the lines the diff adds, with leak_guard's own rules and modes."""
    lg = _leak_module()
    if lg is None:
        return [], [], "skipped: scripts/leak_guard.py not found"
    rules = lg._build_rules(private_dir)
    cfg = lg.settings(private_dir)
    block: list[Finding] = []
    note: list[Finding] = []
    for dl in added:
        if lg.is_skipped(dl.file) or lg._allowed(dl.file, [str(p).replace("\\", "/") for p in cfg["allow"]]):
            continue
        for f in lg.scan_text(dl.file, dl.text, rules, dl.line):
            item = Finding("leak_guard", f"{f.category}/{f.rule}: {f.match!r}", dl.file, dl.line, f.category, str(f.match), dl.text)
            (block if cfg["modes"].get(f.category, "fail") == "fail" else note).append(item)
    return block, note, "fail" if block else "pass"


def check_hotreload(repo: Path, head: str, file_changes: list[Change]) -> tuple[list[Finding], str]:
    src = _show(repo, head, "bot/hotreload.py")
    if src is None:
        return [], "skipped: no bot/hotreload.py"
    out = []
    for c in file_changes:
        p = c.path.replace("\\", "/")
        if c.status not in ("A", "R") or not (p.startswith("bot/") and p.endswith(".py")) or p.endswith("/__init__.py"):
            continue
        dotted = p[:-3].replace("/", ".")
        if f'"{dotted}"' not in src:
            out.append(Finding("hotreload", f"new module {dotted} is not classified in bot/hotreload.py (DENYLIST or RELOAD_ORDER)",
                               p, 0, "unclassified module", dotted))
    return out, "fail" if out else "pass"


def check_tests(file_changes: list[Change]) -> tuple[list[Finding], str]:
    def is_test(p: str) -> bool:
        p = p.replace("\\", "/")
        return p.startswith("tests/") or "/tests/" in p or Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py")

    code = [c for c in file_changes if c.status != "D" and Path(c.path).suffix.lower() in CODE_EXT and not is_test(c.path)
            and not c.path.replace("\\", "/").startswith(("docs/", "examples/", "scripts/"))]
    if not code:
        return [], "pass (no code changed)"
    if any(is_test(c.path) for c in file_changes):
        return [], "pass"
    return [Finding("tests", f"code changed ({len(code)} file(s)) and no test was added or changed", code[0].path, 0, "no tests",
                    ", ".join(c.path for c in code[:5]))], "fail"


def check_ruff(repo: Path, head: str, file_changes: list[Change]) -> tuple[list[Finding], str]:
    py = [c.path for c in file_changes if c.status in ("A", "M", "R") and c.path.endswith(".py")]
    if not py:
        return [], "pass (no Python changed)"
    tmp = Path(tempfile.mkdtemp(prefix="abp-conductor-ruff-"))
    try:
        has_cfg = False
        for cfg in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
            text = _show(repo, head, cfg)
            if text is not None:
                (tmp / cfg).write_text(text, encoding="utf-8")
                has_cfg = True
        for rel in py:
            src = _show(repo, head, rel)
            if src is not None:
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(src, encoding="utf-8")
        proc = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format=concise",
                               *([] if has_cfg else ["--isolated"]), *py], cwd=tmp,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if "No module named ruff" in proc.stderr:
            return [], "skipped: ruff is not installed"
        if proc.returncode == 0:
            return [], "pass"
        out = []
        for ln in proc.stdout.splitlines():
            m = re.match(r"(.+?):(\d+):\d+: (.+)", ln)
            if m:
                out.append(Finding("ruff", m.group(3), m.group(1).replace("\\", "/"), int(m.group(2)), "lint", "", ""))
        if not out:
            out.append(Finding("ruff", (proc.stdout or proc.stderr).strip()[:300] or "ruff failed", kind="lint"))
        return out[:40], "fail"
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"skipped: ruff could not run ({type(exc).__name__})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_scope(root: Path, diff: str, declared: Iterable[str], changed: Iterable[str]) -> tuple[list[Finding], str]:
    """codeintel impact on the diff: callers/importers of the changed code that are not among the task's declared files.
    `root` is a checkout of the head. Findings (one per outside file, capped) are a scope warning for the caller to weigh."""
    decl = {d.replace("\\", "/") for d in declared}
    if not decl:
        return [], "skipped: the task declares no files"
    try:
        from bot.codeintel import tools

        data = tools.impact(Path(root), diff, max_tokens=4000).data or {}
    except Exception as exc:  # noqa: BLE001 - no index, no verdict
        return [], f"skipped: impact unavailable ({type(exc).__name__})"
    own = set(changed) | decl
    outside = [f for f in data.get("affected_files", []) if f not in own and not re.search(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.py$)", f)]
    if not outside:
        return [], "pass"
    syms = ", ".join(data.get("symbols", [])[:3])
    return [Finding("scope", f"changed code ({syms}) is used by {f}, which is not among the task's declared files", f, 0, "scope")
            for f in outside[:10]], "warn"


# ------------------------------------------------------------ the review
ModelCall = Callable[..., Awaitable[tuple[str, str]]]

MODEL_PROMPT = """You review a code change against these rules: never remove a feature (a route, command, flag, tool, page, setting);
do not delete files you did not create; no machine paths, user names or private project names in a public repo; keep the change
small and on the goal; tests for new behaviour.

GOAL: {goal}

DIFF (may be cut):
{diff}

Reply with ONLY JSON: {{"verdict": "accept" or "reject", "concerns": ["short, specific, each naming a file"]}}"""


def review(repo: Any, base: str, head: str, *, created_files: Iterable[str] = (), allow_delete: Iterable[str] = (),
           public: bool = True, private_dir: Optional[Path] = None, run_ruff: bool = True, check_reload: bool = True,
           declared_files: Optional[Iterable[str]] = None, scope_root: Optional[Path] = None, scope_fail: bool = False) -> Review:
    """The static review of base..head in `repo`. Pure: nothing in the repository changes."""
    repo = Path(repo)
    rep = Review(base=_git(repo, "rev-parse", base).strip(), head=_git(repo, "rev-parse", head).strip())
    file_changes = changes(repo, base, head)
    rep.files = [c.path for c in file_changes]
    removed, added = parse_diff(_git(repo, "diff", "-U0", "-M", "--no-color", "--no-ext-diff", base, head))

    def put(rule: str, found: list[Finding], state: Optional[str] = None) -> None:
        rep.findings += found
        rep.checks[rule] = state or ("fail" if found else "pass")

    put("no_feature_removed", check_features(repo, base, head, removed, added, file_changes))
    put("no_deletions", check_deletions(file_changes, created_files, allow_delete))
    if public:
        block, note, state = check_leaks(added, Path(private_dir) if private_dir else None)
        rep.advisory += note
        put("leak_guard", block, state)
    else:
        rep.checks["leak_guard"] = "skipped: not a public repo"
    if check_reload:
        found, state = check_hotreload(repo, head, file_changes)
        put("hotreload", found, state)
    found, state = check_tests(file_changes)
    put("tests", found, state)
    if run_ruff:
        found, state = check_ruff(repo, head, file_changes)
        put("ruff", found, state)
    if declared_files is not None and scope_root is not None:
        found, state = check_scope(Path(scope_root), _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, head),
                                   declared_files, rep.files)
        if scope_fail:
            put("scope", found, state if not found else "fail")
        else:
            rep.advisory += found
            rep.checks["scope"] = state
    rep.verdict = REJECT if rep.findings else ACCEPT
    return rep


async def model_review(rep: Review, repo: Any, *, goal: str = "", complete: Optional[ModelCall] = None, model: str = "",
                       avoid: str = "", diff_cap: int = 12000) -> Review:
    """Add a local model's review to `rep` (a separate call, a capped prompt). Concerns are advisory; a reject verdict with
    concerns rejects. A model that cannot be reached leaves the static verdict as it is."""
    from bot.conductor import llm
    from bot.conductor.planner import extract_json

    complete = complete or llm.complete
    try:
        diff = _git(Path(repo), "diff", "-U2", "--no-color", "--no-ext-diff", rep.base, rep.head)[:max(500, int(diff_cap))]
        text, ref = await complete(MODEL_PROMPT.format(goal=(goal or "(not given)")[:600], diff=diff),
                                   system="You are a strict code reviewer. Answer with JSON only.", max_tokens=600, model=model, avoid=avoid)
        data = extract_json(text)
        concerns = [str(c)[:300] for c in (data.get("concerns") or []) if c][:8] if isinstance(data, dict) else []
        rep.model = ref
        if isinstance(data, dict) and str(data.get("verdict", "")).lower() == "reject" and concerns:
            rep.findings += [Finding("model", c, kind="model review") for c in concerns]
            rep.checks["model"] = "fail"
            rep.verdict = REJECT
        else:
            rep.advisory += [Finding("model", c, kind="model review") for c in concerns]
            rep.checks["model"] = "pass"
    except Exception as exc:  # noqa: BLE001 - an unreachable model leaves the static verdict standing
        rep.checks["model"] = f"skipped: {type(exc).__name__}: {exc}"[:200]
    return rep


def dumps(rep: Review) -> str:
    return json.dumps(rep.to_dict(), indent=1)
