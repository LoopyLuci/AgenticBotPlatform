"""Test selection (Local Factory LF6): run only the tests a change can affect, and say why.

    sel = select(root, changed_paths)                  # -> Selection(full, reasons, tests, why)
    sel.pytest_args()                                  # the test files to hand pytest ([] when sel.full: the whole suite)
    workers = worker_count(len(sel.tests))             # a capped pytest -n (0 = serial)

How a test is chosen for a changed file:
  * the import graph: every Python file under the indexed roots is parsed (ast, no import is executed); a test is selected when it
    reaches a changed module through imports (parent packages included), at most MAX_DEPTH imports away and not through an
    aggregator (HUB_DEPS). ABP's full import closure is dense (the dashboard app imports nearly everything), so the walk is a
    deliberate heuristic: a selected run is a quick gate, and the whole suite before every deploy and on a schedule is the guarantee. String literals that name a known module
    ("bot.x.y") or a known script file ("release_guard.py") count as imports, so importlib/spec_from_file_location users are found.
  * coverage: when a `.coverage` file recorded per-test contexts (pytest --cov-context=test), the tests that executed a changed
    file are added. Coverage only ever adds tests, it never removes one.
  * a non-Python file selects the tests (and the importers of the modules) whose source names its path or file name.

The selection is never trusted when it can be wrong. `full` is set (the whole suite runs) for: test infrastructure (conftest.py,
pytest.ini, pyproject.toml, requirements, tests/fixtures, tests/data), configuration (config/, root-level files), a file type the
selector does not understand that no test names, a Python file outside the indexed roots that no test names, an unknown change
set, a selection that is most of the suite anyway, and a schedule (every N selected runs or H hours, see `ScheduleState`).
Nothing here runs pytest; the caller does (scripts/local_pipeline.py, the conductor's integrator).
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

INDEXED_ROOTS = ("bot", "scripts", "tests")          # plus every top-level abp_* package and root-level *.py
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "target", "build", "dist", ".pytest_cache", "fixtures", "data"}

# Changes the selector cannot reason about: they shape every test (collection, fixtures, dependencies, settings).
FULL_NAMES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "requirements.lock", ".coveragerc"}
FULL_PREFIXES = ("tests/fixtures/", "tests/data/", "config/")
# Files no Python test can depend on unless one names them: docs, images, and the sources other pipeline gates own.
INERT_EXTS = {".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".pdf",
              ".rs", ".kt", ".kts", ".java", ".swift", ".gradle", ".lock"}
MAX_DEPTH = 3             # imports between a changed file and a test that still select it
HUB_DEPS = 20             # a file importing more than this many others is an aggregator the walk does not pass through
FULL_FRACTION = 0.6       # a selection holding more than this share of the suite's test files runs the whole suite instead

_BASENAME_MIN = 6         # a file name shorter than this is too generic to search test sources for
GENERIC_NAMES = {"README.md", "CHANGELOG.md", "LICENSE", "index.html", "index.js", "index.ts", "main.rs", "lib.rs", "mod.rs",
                 "Cargo.toml", "Cargo.lock", "package.json", "package-lock.json", "__init__.py", "__main__.py", "main.py",
                 "config.py", "settings.json", "styles.css", "style.css", "build.gradle", "Makefile", "Dockerfile"}


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


@dataclass
class Selection:
    full: bool
    reasons: list[str] = field(default_factory=list)            # why the whole suite runs (empty when selected)
    tests: list[str] = field(default_factory=list)              # selected test files (empty when full)
    why: dict[str, str] = field(default_factory=dict)           # test file -> the changed file (and how) that selected it
    changed: list[str] = field(default_factory=list)
    total_tests: int = 0
    seconds: float = 0.0

    def pytest_args(self) -> list[str]:
        return [] if self.full else list(self.tests)

    def summary(self) -> str:
        if self.full:
            return "full suite: " + "; ".join(self.reasons)
        if not self.tests:
            return f"no tests affected by {len(self.changed)} changed file(s)"
        return f"{len(self.tests)} of {self.total_tests} test files selected for {len(self.changed)} changed file(s)"

    def report_lines(self, limit: int = 40) -> list[str]:
        lines = [self.summary()]
        for t in self.tests[:limit]:
            lines.append(f"  {t}  <- {self.why.get(t, '?')}")
        if len(self.tests) > limit:
            lines.append(f"  ... and {len(self.tests) - limit} more")
        return lines

    def to_dict(self) -> dict:
        return {"full": self.full, "reasons": self.reasons, "tests": self.tests, "why": self.why, "changed": self.changed,
                "total_tests": self.total_tests, "seconds": round(self.seconds, 2)}


# ------------------------------------------------------------------ graph
class Graph:
    """The repository's Python files, their module names, and who imports whom (file -> files it depends on)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.files: dict[str, str] = {}                 # rel -> source text
        self.modules: dict[str, str] = {}               # dotted name -> rel
        self.bare: dict[str, set[str]] = {}             # top-level name (script / tests helper imported bare) -> rels
        self.basenames: dict[str, set[str]] = {}        # "x.py" -> rels
        self.deps: dict[str, set[str]] = {}
        self.rdeps: dict[str, set[str]] = {}
        self._load()
        self._link()

    def indexed(self, rel: str) -> bool:
        top = rel.split("/", 1)[0]
        return ("/" not in rel and rel.endswith(".py")) or top in INDEXED_ROOTS or top.startswith("abp_")

    def _roots(self) -> list[Path]:
        out = [self.root / r for r in INDEXED_ROOTS]
        out += [p for p in self.root.glob("abp_*") if p.is_dir()]
        return [p for p in out if p.is_dir()]

    def _load(self) -> None:
        paths = [p for p in self.root.glob("*.py") if p.is_file()]
        for base in self._roots():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                paths += [Path(dirpath) / f for f in filenames if f.endswith(".py")]
        for p in paths:
            rel = p.relative_to(self.root).as_posix()
            try:
                self.files[rel] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in module_names(rel):
                self.modules.setdefault(name, rel)
            stem = rel.rsplit("/", 1)[-1][:-3]
            if rel.startswith(("scripts/", "tests/")) and stem != "__init__":
                self.bare.setdefault(stem, set()).add(rel)
            self.basenames.setdefault(rel.rsplit("/", 1)[-1], set()).add(rel)

    def resolve(self, name: str) -> set[str]:
        """The files importing `name` executes: the module itself and its parent packages' __init__."""
        out: set[str] = set()
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            rel = self.modules.get(".".join(parts[:i]))
            if rel:
                out.add(rel)
        if not out and parts[0] in self.bare:
            out |= self.bare[parts[0]]
        return out

    def _imports(self, rel: str, text: str) -> set[str]:
        try:
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, ValueError):
            return set()
        pkg = rel[:-3].split("/")
        pkg = pkg[:-1]                                  # the package a relative import is relative to
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    out |= self.resolve(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
                else:
                    mod = node.module or ""
                if mod:
                    out |= self.resolve(mod)
                for a in node.names:
                    if a.name != "*" and mod:
                        out |= self.resolve(f"{mod}.{a.name}") if f"{mod}.{a.name}" in self.modules else set()
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and 3 < len(node.value) < 200:
                s = node.value.strip()
                if s in self.modules and "." in s:
                    out.add(self.modules[s])
                elif s.endswith(".py"):
                    tail = s.replace("\\", "/").rsplit("/", 1)[-1]
                    if s.replace("\\", "/") in self.files:
                        out.add(s.replace("\\", "/"))
                    elif tail in self.basenames and len(tail) >= _BASENAME_MIN:
                        out |= self.basenames[tail]
        out.discard(rel)
        return out

    def _link(self) -> None:
        for rel, text in self.files.items():
            deps = self._imports(rel, text)
            self.deps[rel] = deps
            for d in deps:
                self.rdeps.setdefault(d, set()).add(rel)

    def test_files(self) -> list[str]:
        return sorted(r for r in self.files if is_test_file(r))

    def naming(self, path: str) -> set[str]:
        """Indexed files whose source names `path` (its repo path, or its file name when that is distinctive)."""
        name = path.rsplit("/", 1)[-1]
        needles = [path] + ([name] if len(name) >= _BASENAME_MIN and name not in GENERIC_NAMES else [])
        return {r for r, text in self.files.items() if r != path and any(n in text for n in needles)}

    def dependents(self, starts: Iterable[str], *, max_depth: int = 0, hub_deps: int = 0) -> dict[str, str]:
        """Every file that reaches one of `starts` through imports -> the start it reached (breadth first, so the nearest).

        max_depth > 0 stops that many imports away; hub_deps > 0 does not walk on through an aggregator (a file importing more
        than that many others, such as an app factory or a module registry): what imports a hub is not exercising the change."""
        seen: dict[str, tuple[str, int]] = {}
        queue = []
        for s in starts:
            if s not in seen:
                seen[s] = (s, 0)
                queue.append(s)
        while queue:
            cur = queue.pop(0)
            start, depth = seen[cur]
            if cur != start and hub_deps and len(self.deps.get(cur, ())) > hub_deps:
                continue
            if max_depth and depth >= max_depth:
                continue
            for nxt in self.rdeps.get(cur, ()):
                if nxt not in seen:
                    seen[nxt] = (start, depth + 1)
                    queue.append(nxt)
        return {k: v[0] for k, v in seen.items()}


def module_names(rel: str) -> list[str]:
    """Dotted names a repo file is importable as ('bot/x/__init__.py' -> 'bot.x'; tests/ and scripts/ files also bare)."""
    if not rel.endswith(".py"):
        return []
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts)] if parts else []


# --------------------------------------------------------------- coverage
def coverage_tests(root: Path, changed: Iterable[str], data_file: Optional[Path] = None) -> dict[str, str]:
    """Test files whose recorded coverage context executed a changed file -> that file. {} without per-test contexts."""
    path = Path(data_file) if data_file else Path(root) / ".coverage"
    if not path.is_file():
        return {}
    wanted = {c for c in changed if c.endswith(".py")}
    if not wanted:
        return {}
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            files = conn.execute("SELECT id, path FROM file").fetchall()
            ids: dict[int, str] = {}
            root_s = str(Path(root).resolve()).replace("\\", "/").rstrip("/") + "/"
            for fid, p in files:
                p = str(p).replace("\\", "/")
                rel = p[len(root_s):] if p.lower().startswith(root_s.lower()) else p
                if rel in wanted:
                    ids[fid] = rel
            if not ids:
                return {}
            q = ("SELECT DISTINCT lb.file_id, c.context FROM line_bits lb JOIN context c ON c.id = lb.context_id "
                 f"WHERE lb.file_id IN ({','.join('?' * len(ids))})")
            for fid, ctx in conn.execute(q, list(ids)):
                test = str(ctx or "").split("::", 1)[0].replace("\\", "/")
                if test and is_test_file(test):
                    out.setdefault(test, ids[fid])
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return out


# --------------------------------------------------------------- schedule
@dataclass
class ScheduleState:
    """When the whole suite last ran, and how many selected runs came since. Kept in a small JSON file by the caller."""
    last_full: float = 0.0
    selected_since: int = 0

    @classmethod
    def load(cls, path: Optional[Path]) -> "ScheduleState":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
            return cls(float(data.get("last_full", 0.0)), int(data.get("selected_since", 0)))
        except (OSError, ValueError, TypeError):
            return cls()

    def due(self, *, every_runs: int, every_hours: float, now: Optional[float] = None) -> Optional[str]:
        now = time.time() if now is None else now
        if not self.last_full:
            return "no full run on record yet"
        if every_runs > 0 and self.selected_since >= every_runs:
            return f"scheduled: {self.selected_since} selected runs since the last full run (every {every_runs})"
        if every_hours > 0 and now - self.last_full >= every_hours * 3600:
            return f"scheduled: the last full run was {(now - self.last_full) / 3600:.0f} h ago (every {every_hours:g} h)"
        return None

    def record(self, path: Optional[Path], *, full: bool, passed: bool, now: Optional[float] = None) -> None:
        """A full run counts only when it passed; a selected run counts towards the next scheduled full run."""
        if full and passed:
            self.last_full, self.selected_since = (time.time() if now is None else now), 0
        elif not full:
            self.selected_since += 1
        if path:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(json.dumps({"last_full": self.last_full, "selected_since": self.selected_since}),
                                      encoding="utf-8")
            except OSError:
                pass


def schedule_settings() -> tuple[int, float]:
    """(every N selected runs, every H hours) from ABP_TESTSELECT_FULL_EVERY_RUNS / _HOURS; 0 turns that trigger off."""
    def num(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return int(num("ABP_TESTSELECT_FULL_EVERY_RUNS", 10)), num("ABP_TESTSELECT_FULL_EVERY_HOURS", 24)


# --------------------------------------------------------------- workers
def worker_cap(cpus: Optional[int] = None) -> int:
    """The most pytest-xdist workers any run gets: ABP_PYTEST_MAX_WORKERS, else half the logical CPUs, at most 8.

    The owner's CPU has had machine-check crashes under long all-core load, so `-n auto` (every core) is never used."""
    env = os.environ.get("ABP_PYTEST_MAX_WORKERS", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    if cpus is None:
        cpus = os.cpu_count() or 2
    return max(1, min(8, cpus // 2))


def worker_count(n_test_files: Optional[int], cpus: Optional[int] = None) -> int:
    """pytest -n for a run of `n_test_files` files (None = the whole suite): 0 means serial (no xdist)."""
    cap = worker_cap(cpus)
    if n_test_files is None:
        return cap
    if n_test_files <= 3 or cap <= 1:
        return 0
    return min(cap, max(2, n_test_files // 3))


# ---------------------------------------------------------------- select
def classify(path: str) -> Optional[str]:
    """Why a changed path forces the whole suite on its own, or None."""
    name = path.rsplit("/", 1)[-1]
    if name in FULL_NAMES or name.startswith("requirements") and name.endswith((".txt", ".in")):
        return f"{path}: test infrastructure / dependencies"
    if path.startswith(FULL_PREFIXES):
        return f"{path}: fixtures, test data or configuration"
    return None


def select(root: Path, changed: Optional[Iterable[str]], *, graph: Optional[Graph] = None, coverage: bool = True,
           schedule: Optional[ScheduleState] = None, every_runs: int = 0, every_hours: float = 0.0,
           force_full: str = "", max_depth: int = MAX_DEPTH, hub_deps: int = HUB_DEPS) -> Selection:
    """The tests `changed` (repo-relative paths) can affect. None = unknown change set = the whole suite."""
    t0 = time.time()
    root = Path(root)
    if changed is None:
        return Selection(True, ["the changed files could not be determined"], seconds=time.time() - t0)
    changed = sorted({c.strip().replace("\\", "/") for c in changed if c and c.strip()})
    reasons: list[str] = [force_full] if force_full else []
    if schedule is not None:
        due = schedule.due(every_runs=every_runs, every_hours=every_hours)
        if due:
            reasons.append(due)
    reasons += [r for r in (classify(c) for c in changed) if r]
    if reasons:
        return Selection(True, reasons, changed=changed, seconds=time.time() - t0)

    g = graph or Graph(root)
    starts: dict[str, str] = {}                # graph node -> the changed path (and how) it stands for
    named_starts: dict[str, str] = {}          # modules that name a changed non-Python file: only their direct importers count
    for c in changed:
        ext = os.path.splitext(c)[1].lower()
        if c.endswith(".py") and g.indexed(c):
            starts.setdefault(c, c)
            continue
        named = g.naming(c)
        if named:
            for r in named:
                (starts if is_test_file(r) else named_starts).setdefault(r, f"{c} (named in {r})")
        elif c.endswith(".py"):
            reasons.append(f"{c}: Python outside the indexed roots and no test names it")
        elif "/" not in c and ext not in (".md", ".txt"):
            reasons.append(f"{c}: a root-level file no test names")
        elif ext not in INERT_EXTS:
            reasons.append(f"{c}: a file type the selector does not understand ({ext or 'no extension'}) and no test names it")
    if reasons:
        return Selection(True, reasons, changed=changed, seconds=time.time() - t0)

    all_tests = g.test_files()
    why: dict[str, str] = {}
    reach = g.dependents(starts, max_depth=max_depth, hub_deps=hub_deps)
    for rel, start in g.dependents(named_starts, max_depth=1, hub_deps=hub_deps).items():
        reach.setdefault(rel, start)
    origin = {**named_starts, **starts}
    for rel, start in reach.items():
        if is_test_file(rel):
            src = origin[start]
            why[rel] = ("changed" if src == rel else src) if rel == start else f"imports {src}"
    if coverage:
        for t, src in coverage_tests(root, changed).items():
            if (root / t).is_file():
                why.setdefault(t, f"covers {src} (coverage)")
    tests = sorted(t for t in why if (root / t).is_file())
    why = {t: why[t] for t in tests}
    if all_tests and len(tests) > FULL_FRACTION * len(all_tests):
        return Selection(True, [f"the change reaches {len(tests)} of {len(all_tests)} test files (most of the suite)"],
                         changed=changed, total_tests=len(all_tests), seconds=time.time() - t0)
    return Selection(False, [], tests, why, changed, len(all_tests), time.time() - t0)


def git_changed(root: Path, base: str = "origin/main", head: str = "HEAD", worktree: bool = False) -> Optional[list[str]]:
    """Files changed between merge-base(base, head) and head (plus uncommitted ones with worktree=True); None on a git error."""
    def git(*args: str) -> Optional[str]:
        try:
            p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None
    mb = git("merge-base", base, head)
    if mb is None:
        return None
    out = git("diff", "--name-only", mb.strip(), head)
    if out is None:
        return None
    files = set(out.split())
    if worktree:
        extra = git("status", "--porcelain", "--untracked-files=all")
        if extra is None:
            return None
        files |= {re.sub(r"^.* -> ", "", line[3:]).strip().strip('"') for line in extra.splitlines() if len(line) > 3}
    return sorted(f.replace("\\", "/") for f in files if f)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m bot.conductor.testselect", description=__doc__.split("\n", 1)[0])
    ap.add_argument("paths", nargs="*", help="changed paths (default: git diff against --base, uncommitted files included)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--run", action="store_true", help="run pytest on the selection (capped workers)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    changed = a.paths or git_changed(root, a.base, worktree=True)
    sel = select(root, changed)
    if a.json:
        print(json.dumps(sel.to_dict(), indent=2))
    else:
        print("\n".join(sel.report_lines()))
    if not a.run or (not sel.full and not sel.tests):
        return 0
    n = worker_count(None if sel.full else len(sel.tests))
    cmd = [sys.executable, "-m", "pytest", "-q", "-rfE", *(["-n", str(n)] if n else ["-p", "no:xdist"]), *sel.pytest_args()]
    sys.stdout.flush()
    return subprocess.call(cmd, cwd=root)


if __name__ == "__main__":
    sys.exit(main())
