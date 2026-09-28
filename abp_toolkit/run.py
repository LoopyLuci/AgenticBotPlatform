"""Running code in many languages: a snippet in a scratch folder, or a file in the working folder.

Each language uses its real interpreter or compiler when it is installed: Python, Node (JavaScript, and TypeScript
through tsx / ts-node / deno), PowerShell, CMD, VBScript and JScript (cscript), Bash, Go, Rust, C and C++ (gcc, clang
or MSVC's cl), C#, Java, Kotlin script, Ruby, PHP, Lua, Perl, R, Dart, SQLite SQL. Every run has a time limit and a
cap on output, and reports the exit code, stdout, stderr and how long it took.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, run as run_proc, which

group("run", "Run code in any installed language: snippets, files, with time limits")

MAX_OUT = 60_000
EXE = ".exe" if os.name == "nt" else ""


def _python() -> list[str]:
    return [sys.executable]


# language -> (file name, how to build the command from the file path, programs needed)
RUNNERS: dict[str, tuple[str, Callable[[Path], Optional[list]], str]] = {
    "python": ("main.py", lambda f: [sys.executable, "-X", "utf8", f], "python"),
    "javascript": ("main.js", lambda f: [which("node"), f] if which("node") else None, "node"),
    "typescript": ("main.ts", lambda f: ([which("tsx"), f] if which("tsx") else [which("ts-node"), f] if which("ts-node")
                                         else [which("deno"), "run", "--quiet", f] if which("deno")
                                         else [which("node"), "--experimental-strip-types", "--no-warnings", f] if which("node") else None),
                   "tsx / ts-node / deno / node 22+"),
    "powershell": ("main.ps1", lambda f: [which("pwsh") or which("powershell"), "-NoProfile", "-NonInteractive",
                                          "-ExecutionPolicy", "Bypass", "-File", f] if (which("pwsh") or which("powershell")) else None,
                   "PowerShell"),
    "batch": ("main.cmd", lambda f: [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", f] if os.name == "nt" else None, "cmd.exe (Windows)"),
    "vbscript": ("main.vbs", lambda f: [which("cscript"), "//nologo", "//E:vbscript", f] if which("cscript") else None, "cscript (Windows)"),
    "jscript": ("main.js", lambda f: [which("cscript"), "//nologo", "//E:jscript", f] if which("cscript") else None, "cscript (Windows)"),
    "shell": ("main.sh", lambda f: [which("bash"), f] if which("bash") else None, "bash"),
    "go": ("main.go", lambda f: [which("go"), "run", f] if which("go") else None, "go"),
    "ruby": ("main.rb", lambda f: [which("ruby"), f] if which("ruby") else None, "ruby"),
    "php": ("main.php", lambda f: [which("php"), f] if which("php") else None, "php"),
    "lua": ("main.lua", lambda f: [which("lua") or which("luajit"), f] if (which("lua") or which("luajit")) else None, "lua"),
    "perl": ("main.pl", lambda f: [which("perl"), f] if which("perl") else None, "perl"),
    "r": ("main.R", lambda f: [which("Rscript"), f] if which("Rscript") else None, "Rscript"),
    "dart": ("main.dart", lambda f: [which("dart"), "run", f] if which("dart") else None, "dart"),
    "kotlin": ("main.kts", lambda f: [which("kotlinc"), "-script", f] if which("kotlinc") else None, "kotlinc"),
    "java": ("Main.java", lambda f: [which("java"), f] if which("java") else None, "java 11+"),
    "sql": ("main.sql", lambda f: [which("sqlite3"), ":memory:", f".read {f.as_posix()}"] if which("sqlite3") else [sys.executable, "-c",
           "import sqlite3,sys;c=sqlite3.connect(':memory:');cur=c.cursor();\n"
           "for s in [x for x in open(sys.argv[1],encoding='utf-8').read().split(';') if x.strip()]:\n"
           " cur.execute(s)\n r=cur.fetchall()\n"
           " [print('|'.join(str(v) for v in row)) for row in r]", f], "sqlite3 (or Python's sqlite)"),
}
COMPILED = {"c": ("main.c", "gcc / clang / cl"), "cpp": ("main.cpp", "g++ / clang++ / cl"), "rust": ("main.rs", "rustc"),
            "csharp": ("Program.cs", "dotnet")}


def _compile(lang: str, src: Path, d: Path, timeout: float) -> tuple[Optional[list], str]:
    out = d / ("app" + EXE)
    if lang in ("c", "cpp"):
        cc = which("gcc" if lang == "c" else "g++") or which("clang" if lang == "c" else "clang++")
        if cc:
            r = run_proc([cc, "-O1", "-o", out, src], cwd=d, timeout=timeout)
            return ([out] if r.ok else None), r.err + r.out
        if which("cl"):
            r = run_proc([which("cl"), "/nologo", "/EHsc", f"/Fe:{out}", src], cwd=d, timeout=timeout)
            return ([out] if r.ok else None), r.out + r.err
        return None, "no C/C++ compiler (install gcc, clang or Visual Studio Build Tools)"
    if lang == "rust":
        if not which("rustc"):
            return None, "rustc is not installed (https://rustup.rs)"
        r = run_proc([which("rustc"), "-O", "-o", out, src], cwd=d, timeout=timeout)
        return ([out] if r.ok else None), r.err
    if lang == "csharp":
        if not which("dotnet"):
            return None, "dotnet is not installed"
        r = run_proc([which("dotnet"), "new", "console", "--force", "-o", d, "-n", "App"], cwd=d, timeout=timeout)
        if not r.ok:
            return None, r.err + r.out
        (d / "Program.cs").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return [which("dotnet"), "run", "--project", d], ""
    return None, f"no compiler for {lang}"


def _limit(text: str) -> str:
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def execute(lang: str, source: Path, cwd: Path, args: list[str], stdin: Optional[str], timeout: float,
            env: Optional[dict]) -> dict:
    t = time.perf_counter()
    if lang in RUNNERS:
        cmd = RUNNERS[lang][1](source)
        if cmd is None:
            raise ToolkitError(f"{lang} needs {RUNNERS[lang][2]}, which is not installed")
        compile_log = ""
    elif lang in COMPILED:
        cmd, compile_log = _compile(lang, source, source.parent, timeout)
        if cmd is None:
            return {"language": lang, "compiled": False, "compile_errors": _limit(compile_log), "exit_code": None}
    else:
        raise ToolkitError(f"cannot run {lang!r}; one of {', '.join(sorted(list(RUNNERS) + list(COMPILED)))}")
    r = run_proc([*cmd, *args], cwd=cwd, timeout=timeout, input=stdin, env={"PYTHONIOENCODING": "utf-8", **(env or {})})
    return {"language": lang, "command": [str(c) for c in cmd][:3], "exit_code": r.code, "timed_out": r.timed_out,
            "stdout": _limit(r.out), "stderr": _limit(r.err), "seconds": round(time.perf_counter() - t, 3),
            **({"compile_output": _limit(compile_log)} if compile_log else {})}


@action("run.snippet", executes=True)
def snippet(code: str, language: str, args: Optional[list[str]] = None, stdin: str = "", timeout_s: float = 60,
            env: Optional[dict] = None) -> dict:
    """Run a piece of code in a fresh scratch folder and return its output

    code: the program
    language: python, javascript, typescript, powershell, batch, vbscript, jscript, shell, go, rust, c, cpp, csharp, java, kotlin, ruby, php, lua, perl, r, dart, sql
    args: command-line arguments
    stdin: text sent to its standard input
    timeout_s: stop it after this many seconds
    env: extra environment variables
    """
    name = (RUNNERS.get(language) or COMPILED.get(language) or ("main.txt",))[0]
    with tempfile.TemporaryDirectory(prefix="abp-run-") as d:
        src = Path(d) / name
        text = code
        if language == "batch" and not code.lstrip().lower().startswith("@echo off"):
            text = "@echo off\r\n" + code
        if language in ("batch", "vbscript", "jscript"):
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        src.write_text(text, encoding="utf-8" if language not in ("batch", "vbscript", "jscript") else "mbcs" if os.name == "nt" else "utf-8")
        return execute(language, src, Path(d), list(args or []), stdin or None, min(timeout_s, 900), env)


@action("run.file", executes=True)
def run_file(workspace: Path, path: str, args: Optional[list[str]] = None, stdin: str = "", timeout_s: float = 120,
             language: str = "", env: Optional[dict] = None) -> dict:
    """Run a script or source file from the working folder (the language is taken from its extension)

    path: the file, inside the working folder
    args: command-line arguments
    stdin: text sent to its standard input
    timeout_s: stop it after this many seconds
    language: override the language worked out from the extension
    """
    from abp_toolkit.util import language_of
    src = inside(workspace, path, must_exist=True)
    lang = language or language_of(src) or ""
    if src.suffix.lower() == ".js" and not language:
        lang = "javascript"
    if lang in COMPILED:
        with tempfile.TemporaryDirectory(prefix="abp-build-") as d:
            copy = Path(d) / src.name
            shutil.copy2(src, copy)
            return execute(lang, copy, src.parent, list(args or []), stdin or None, min(timeout_s, 1800), env)
    return execute(lang, src, src.parent, list(args or []), stdin or None, min(timeout_s, 1800), env)


@action("run.languages")
def languages() -> dict:
    """Which languages can run on this machine right now, and what the others need"""
    out = {}
    for lang, (_n, build, need) in RUNNERS.items():
        out[lang] = {"available": build(Path("x")) is not None, "needs": need}
    for lang, (_n, need) in COMPILED.items():
        avail = {"c": which("gcc") or which("clang") or which("cl"), "cpp": which("g++") or which("clang++") or which("cl"),
                 "rust": which("rustc"), "csharp": which("dotnet")}[lang]
        out[lang] = {"available": bool(avail), "needs": need}
    return out


@action("run.command", executes=True)
def command(workspace: Path, program: str, args: Optional[list[str]] = None, timeout_s: float = 120, stdin: str = "") -> dict:
    """Run one program with arguments in the working folder, without a shell (no pipes or globbing)

    program: the program (on PATH, or a path inside the working folder)
    args: its arguments
    timeout_s: stop it after this many seconds
    """
    exe = which(program) or (str(inside(workspace, program, must_exist=True)) if ("/" in program or "\\" in program) else None)
    if not exe:
        raise ToolkitError(f"{program} is not installed or not on PATH")
    r = run_proc([exe, *(args or [])], cwd=Path(workspace), timeout=min(timeout_s, 1800), input=stdin or None)
    return {"exit_code": r.code, "timed_out": r.timed_out, "stdout": _limit(r.out), "stderr": _limit(r.err)}
