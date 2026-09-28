"""Python, fully: virtual environments, packages, tests, types, profiling, packaging, and the syntax tree.

Everything works on the working folder's own environment when it has one (``.venv`` / ``venv`` / ``env``), or on a
chosen interpreter, so an agent never installs into the wrong Python.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, rel, run, which

group("python", "Python: venvs, packages, tests, type checks, profiling, packaging, AST, dependency audit")


def _venv_python(root: Path) -> Optional[Path]:
    for name in (".venv", "venv", "env"):
        for sub in ("Scripts/python.exe", "bin/python"):
            p = root / name / sub
            if p.is_file():
                return p
    return None


def interpreter(workspace: Path, python: str = "") -> str:
    """The Python to use: the one given, else the working folder's venv, else this toolkit's own."""
    if python:
        if "/" in python or "\\" in python:
            p = inside(workspace, python, must_exist=True)
            return str(p)
        found = which(python)
        if not found:
            raise ToolkitError(f"{python} is not installed")
        return found
    v = _venv_python(Path(workspace))
    return str(v) if v else sys.executable


@action("python.info")
def info(workspace: Path, python: str = "") -> dict:
    """The Python that would be used here: version, path, whether it is a venv, and what is installed in it

    python: an interpreter (name on PATH or a path inside the working folder); default: the folder's venv
    """
    exe = interpreter(workspace, python)
    r = run([exe, "-c", "import sys,json,platform,sysconfig;print(json.dumps({'version':platform.python_version(),"
             "'implementation':platform.python_implementation(),'executable':sys.executable,'prefix':sys.prefix,"
             "'venv':sys.prefix!=sys.base_prefix,'site':sysconfig.get_paths()['purelib']}))"], timeout=30)
    if not r.ok:
        raise ToolkitError(r.err.strip()[-500:])
    data = json.loads(r.out)
    pk = run([exe, "-m", "pip", "list", "--format", "json", "--disable-pip-version-check"], timeout=60)
    try:
        data["packages"] = {p["name"]: p["version"] for p in json.loads(pk.out)}
    except ValueError:
        data["packages"] = {}
    return data


@action("python.venv_create", writes=True, executes=True)
def venv_create(workspace: Path, path: str = ".venv", python: str = "", requirements: str = "",
                upgrade_pip: bool = True) -> dict:
    """Create a virtual environment in the working folder, optionally installing a requirements file into it

    path: where to create it (inside the working folder)
    python: the interpreter to base it on (default: the toolkit's own)
    requirements: a requirements file (inside the working folder) to install
    """
    target = inside(workspace, path)
    if (target / "pyvenv.cfg").exists():
        raise ToolkitError(f"{path} is already a virtual environment")
    base = which(python) if python else (getattr(sys, "_base_executable", "") or sys.executable)
    r = run([base, "-m", "venv", target], timeout=600)
    if not r.ok:
        raise ToolkitError(r.err.strip()[-800:])
    py = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    log = []
    if upgrade_pip:
        u = run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], timeout=600)
        log.append("pip upgraded" if u.ok else u.err.strip()[-300:])
    if requirements:
        req = inside(workspace, requirements, must_exist=True)
        i = run([py, "-m", "pip", "install", "-q", "-r", req], timeout=3600)
        if not i.ok:
            raise ToolkitError(f"venv created but installing {requirements} failed: {i.err.strip()[-800:]}")
        log.append(f"installed {requirements}")
    return {"venv": rel(workspace, target), "python": str(py), "log": log}


@action("python.pip", executes=True, network=True)
def pip(workspace: Path, command: str, packages: Optional[list[str]] = None, python: str = "",
        upgrade: bool = False, requirements: str = "") -> dict:
    """Install, uninstall, upgrade or inspect packages in the working folder's Python (list, outdated, show, freeze, check)

    command: install, uninstall, list, outdated, show, freeze, check, download
    packages: package names or specifiers (requests>=2.31, ./local-package)
    requirements: a requirements file inside the working folder (install / download)
    upgrade: with install, upgrade what is already there
    """
    exe = interpreter(workspace, python)
    base = [exe, "-m", "pip", "--disable-pip-version-check"]
    pk = list(packages or [])
    if command == "install":
        args = ["install", *(["--upgrade"] if upgrade else []), *pk]
        if requirements:
            args += ["-r", str(inside(workspace, requirements, must_exist=True))]
        if len(args) == 1:
            raise ToolkitError("give packages or a requirements file")
    elif command == "uninstall":
        if not pk:
            raise ToolkitError("give the packages to uninstall")
        args = ["uninstall", "-y", *pk]
    elif command == "list":
        args = ["list", "--format", "json"]
    elif command == "outdated":
        args = ["list", "--outdated", "--format", "json"]
    elif command == "show":
        args = ["show", *pk]
    elif command == "freeze":
        args = ["freeze"]
    elif command == "check":
        args = ["check"]
    elif command == "download":
        args = ["download", "-d", str(inside(workspace, "wheelhouse")), *pk]
    else:
        raise ToolkitError("command is install, uninstall, list, outdated, show, freeze, check or download")
    r = run(base + args, cwd=Path(workspace), timeout=3600)
    out: dict = {"command": command, "ok": r.ok, "python": exe}
    if command in ("list", "outdated") and r.ok:
        out["packages"] = json.loads(r.out or "[]")
    else:
        out["output"] = (r.out + r.err)[-8000:]
    return out


@action("python.test", executes=True)
def test(workspace: Path, path: str = "", keyword: str = "", python: str = "", fail_fast: bool = False,
         timeout_s: float = 900, extra_args: Optional[list[str]] = None) -> dict:
    """Run the tests (pytest, or unittest when pytest is not installed) and report passes, failures and why

    path: a test file or folder (default: pytest's own discovery)
    keyword: only tests matching this expression (-k)
    fail_fast: stop at the first failure
    extra_args: more pytest arguments
    """
    exe = interpreter(workspace, python)
    has_pytest = run([exe, "-c", "import pytest"], timeout=30).ok
    target = [str(inside(workspace, path, must_exist=True))] if path else []
    if has_pytest:
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "junit.xml"
            args = [exe, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider", f"--junitxml={report}", *target,
                    *(["-k", keyword] if keyword else []), *(["-x"] if fail_fast else []), *(extra_args or [])]
            r = run(args, cwd=Path(workspace), timeout=timeout_s)
            results = _junit(report) if report.exists() else {}
    else:
        r = run([exe, "-m", "unittest", "discover", "-s", target[0] if target else ".", "-v"], cwd=Path(workspace), timeout=timeout_s)
        results = {}
    summary = next((l for l in reversed((r.out + r.err).splitlines()) if re.search(r"\d+ (passed|failed|error)|^(OK|FAILED)", l)), "")
    return {"runner": "pytest" if has_pytest else "unittest", "exit_code": r.code, "timed_out": r.timed_out,
            "summary": summary.strip("= "), **results, "output_tail": (r.out + r.err)[-6000:]}


def _junit(path: Path) -> dict:
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed = []
    for s in suites:
        for k in counts:
            counts[k] += int(s.get(k, 0) or 0)
        for case in s.iter("testcase"):
            for kind in ("failure", "error"):
                el = case.find(kind)
                if el is not None:
                    failed.append({"test": f"{case.get('classname')}::{case.get('name')}", "kind": kind,
                                   "message": (el.get("message") or "")[:400], "detail": (el.text or "")[-1500:]})
    return {**counts, "failed": failed[:50]}


@action("python.typecheck", executes=True, needs=["mypy", "pyright"])
def typecheck(workspace: Path, path: str = ".", python: str = "", strict: bool = False) -> dict:
    """Type-check Python with mypy (or pyright) and return each error with its place

    path: a file or folder inside the working folder
    strict: mypy --strict
    """
    target = inside(workspace, path, must_exist=True)
    exe = interpreter(workspace, python)
    if run([exe, "-c", "import mypy"], timeout=30).ok:
        r = run([exe, "-m", "mypy", "--show-column-numbers", "--no-error-summary", "--ignore-missing-imports",
                 *(["--strict"] if strict else []), target], cwd=Path(workspace), timeout=1200)
        tool = "mypy"
        pat = r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|note|warning): (?P<msg>.+?)(?:\s+\[(?P<code>[\w-]+)\])?$"
    elif which("pyright"):
        r = run([which("pyright"), target], cwd=Path(workspace), timeout=1200)
        tool = "pyright"
        pat = r"^\s*(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+) - (?P<sev>error|warning|information): (?P<msg>.+?)(?: \((?P<code>\w+)\))?$"
    else:
        raise ToolkitError("no type checker: pip install mypy (or npm install -g pyright)")
    items = [m.groupdict() for m in re.finditer(pat, r.out, re.M)]
    return {"tool": tool, "errors": sum(1 for i in items if i["sev"] == "error"), "items": items[:300]}


@action("python.profile", executes=True)
def profile(workspace: Path, path: str = "", code: str = "", args: Optional[list[str]] = None, python: str = "",
            top: int = 25, timeout_s: float = 300) -> dict:
    """Profile a script (or a snippet) with cProfile: the functions that took the most time

    path: a script inside the working folder
    code: or a snippet to profile instead
    top: how many functions to list
    """
    exe = interpreter(workspace, python)
    with tempfile.TemporaryDirectory() as d:
        stats = Path(d) / "prof.out"
        if code:
            script = Path(d) / "snippet.py"
            script.write_text(code, encoding="utf-8")
        elif path:
            script = inside(workspace, path, must_exist=True)
        else:
            raise ToolkitError("give path or code")
        r = run([exe, "-m", "cProfile", "-o", stats, script, *(args or [])], cwd=Path(workspace), timeout=timeout_s)
        if not stats.exists():
            raise ToolkitError((r.err or r.out).strip()[-1500:] or "the profiler wrote nothing")
        rep = run([exe, "-c", "import pstats,sys,json;s=pstats.Stats(sys.argv[1]);t=s.total_tt;"
                   "rows=sorted(s.stats.items(),key=lambda kv:-kv[1][3])[:int(sys.argv[2])];"
                   "print(json.dumps({'total_s':round(t,4),'rows':[{'function':f'{k[2]} ({k[0].split(chr(92))[-1].split(chr(47))[-1]}:{k[1]})',"
                   "'calls':v[1],'own_s':round(v[2],4),'total_s':round(v[3],4)} for k,v in rows]}))", stats, str(top)], timeout=60)
        data = json.loads(rep.out)
    return {**data, "exit_code": r.code, "stdout_tail": r.out[-2000:], "stderr_tail": r.err[-2000:]}


@action("python.timeit", executes=True)
def timeit(statement: str, setup: str = "pass", number: int = 0, repeat: int = 5) -> dict:
    """Time a Python statement precisely (best of several runs), like python -m timeit

    statement: the code to time
    setup: code run once before (imports, data)
    number: runs per measurement (0 = pick automatically)
    """
    code = ("import timeit,json,sys;t=timeit.Timer(sys.argv[1],sys.argv[2]);n=int(sys.argv[3]) or t.autorange()[0];"
            "r=t.repeat(int(sys.argv[4]),n);print(json.dumps({'number':n,'best_s':min(r)/n,'mean_s':sum(r)/len(r)/n,'runs':[x/n for x in r]}))")
    r = run([sys.executable, "-c", code, statement, setup, str(number), str(repeat)], timeout=600)
    if not r.ok:
        raise ToolkitError(r.err.strip()[-1500:])
    d = json.loads(r.out)
    d["best_human"] = _human(d["best_s"])
    return d


def _human(s: float) -> str:
    for unit, f in (("s", 1), ("ms", 1e3), ("µs", 1e6), ("ns", 1e9)):
        if s * f >= 1:
            return f"{s * f:.3g} {unit}"
    return f"{s * 1e9:.3g} ns"


@action("python.ast")
def ast_dump(code: str, mode: str = "exec", include_attributes: bool = False) -> dict:
    """The syntax tree of Python code, for understanding exactly how it parses

    code: Python source
    mode: exec, eval or single
    """
    try:
        tree = ast.parse(code, mode=mode)
    except SyntaxError as e:
        raise ToolkitError(f"syntax error on line {e.lineno}, column {e.offset}: {e.msg}") from e
    return {"dump": ast.dump(tree, indent=1, include_attributes=include_attributes)[:60_000]}


@action("python.eval")
def safe_eval(expression: str) -> dict:
    """Evaluate a Python literal or arithmetic expression safely (no names, calls or attributes)

    expression: e.g. 2**64 - 1, [1, 2] + [3], {"a": 1}["a"], 0xff & 0b1010
    """
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.List, ast.Tuple,
               ast.Set, ast.Dict, ast.Subscript, ast.Slice, ast.IfExp, ast.operator, ast.unaryop, ast.boolop,
               ast.cmpop, ast.expr_context, ast.Load)
    for n in ast.walk(tree):
        if not isinstance(n, allowed):
            raise ToolkitError(f"{type(n).__name__} is not allowed: only literals and operators")
    value = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})  # noqa: S307 - the tree was checked above
    return {"value": value if isinstance(value, (int, float, str, bool, list, dict, type(None))) else repr(value),
            "type": type(value).__name__}


@action("python.audit", executes=True, network=True, needs=["pip-audit"])
def audit(workspace: Path, requirements: str = "", python: str = "") -> dict:
    """Known security vulnerabilities in the installed packages (or a requirements file), via pip-audit

    requirements: a requirements file inside the working folder (default: what is installed)
    """
    exe = interpreter(workspace, python)
    if not run([exe, "-c", "import pip_audit"], timeout=30).ok and not which("pip-audit"):
        raise ToolkitError("pip-audit is not installed (python.pip install pip-audit)")
    base = [exe, "-m", "pip_audit"] if run([exe, "-c", "import pip_audit"], timeout=30).ok else [which("pip-audit")]
    args = base + ["--format", "json", "--progress-spinner", "off"]
    if requirements:
        args += ["-r", str(inside(workspace, requirements, must_exist=True))]
    r = run(args, cwd=Path(workspace), timeout=1800)
    try:
        data = json.loads(r.out)
    except ValueError:
        raise ToolkitError((r.err or r.out).strip()[-1500:]) from None
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    vulns = [{"package": d["name"], "version": d["version"], "id": v["id"], "fix": v.get("fix_versions"),
              "description": (v.get("description") or "")[:300]} for d in deps for v in d.get("vulns", [])]
    return {"packages_checked": len(deps), "vulnerable": len({v["package"] for v in vulns}), "vulnerabilities": vulns}


@action("python.package", writes=True, executes=True)
def package(workspace: Path, path: str = ".", python: str = "") -> dict:
    """Build a Python project's wheel and source distribution (python -m build) into dist/

    path: the project folder (with pyproject.toml or setup.py)
    """
    proj = inside(workspace, path, must_exist=True)
    if not ((proj / "pyproject.toml").exists() or (proj / "setup.py").exists()):
        raise ToolkitError(f"{path} has no pyproject.toml or setup.py")
    exe = interpreter(workspace, python)
    if not run([exe, "-c", "import build"], timeout=30).ok:
        i = run([exe, "-m", "pip", "install", "-q", "build"], timeout=900)
        if not i.ok:
            raise ToolkitError("could not install the 'build' package: " + i.err.strip()[-400:])
    r = run([exe, "-m", "build", proj], cwd=proj, timeout=1800)
    if not r.ok:
        raise ToolkitError((r.err + r.out).strip()[-2000:])
    dist = proj / "dist"
    return {"built": [rel(workspace, p) for p in sorted(dist.glob("*"))] if dist.exists() else [], "log_tail": r.out[-1500:]}
