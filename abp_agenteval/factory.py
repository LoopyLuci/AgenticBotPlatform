"""The factory bench: real, already-solved tasks from this repository's own history, replayed against an agent.

    python -m abp_agenteval factory list
    python -m abp_agenteval factory verify                      # each task: its tests fail at the base, pass with the real fix
    python -m abp_agenteval factory run --base-url http://127.0.0.1:8080/v1 --model NAME --context 32768 --out report.json

A task (abp_agenteval/factory_tasks.json) is a commit that fixed or added something with a test. The agent gets a
throwaway git worktree of the commit's PARENT and a prompt written like an issue (what is wrong or wanted, never the
solution). When it stops, the judge lays the commit's own tests over the worktree and runs them (plus ruff on the files
the agent touched and the task's text checks). The worktree is removed afterwards.

What is graded, per task: tests pass, ruff clean, files touched that the real change did not touch (scope), seconds,
tokens, tool calls that failed, and - when it failed - one failure kind:

    timeout           the per-task time limit ended the run
    context overflow  the server refused the conversation as too long
    bad tool call     tool calls that failed or arrived as text, and no usable edit came of it
    gave up           the agent stopped without changing anything
    wrong edit        it changed files, the tests still fail
    provider error    the endpoint answered an error (429, 5xx, connection) - the model was not measured
    lint / scope      the tests pass but ruff fails / the agent also changed unrelated files (only counted in `clean`)

No task data holds a machine path: files are repository-relative, commits are ids (with the subject, so a rewritten
history can still be matched). Where the agent runs is chosen by the caller (`--workdir`, default the system temp folder).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import SCHEMA_VERSION
from .runner import LIMIT_PATTERN, _discard
from .task import Check

DATA_FILE = Path(__file__).with_name("factory_tasks.json")
REPO = Path(__file__).resolve().parent.parent
PREFIX = "bench-"
KINDS = ("bugfix", "flaky", "feature", "docs", "sdk", "hotreload", "config")
FAILURE_KINDS = ("timeout", "context overflow", "bad tool call", "gave up", "wrong edit", "provider error", "lint", "scope")
CONTEXT_PATTERN = re.compile(r"context (length|window|size)|too long|exceed[s]? (the )?(available )?context|n_ctx|maximum context|"
                             r"prompt is too large|input is too long", re.I)
BAD_CALL_PATTERN = re.compile(r"failed to parse tool call|invalid tool call|tool call arguments", re.I)
TIMEOUT_PATTERN = re.compile(r"timed out|timeout|time limit|max_seconds", re.I)
TEXT_TOOL_CALL = re.compile(r"<tool_call>|<function=|<\|tool_call|\"name\"\s*:\s*\"(?:edit_file|read_file|write_file|apply_patch)\"", re.I)
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")
# The tools the bench agent is told about (the confined dev swarm's set: file and search tools, no network, no Python
# runner of its own). `run_shell` is added only when the caller has a sandbox for it; without one the agent gets
# `run_tests` (pytest on named test paths, nothing else) so it can still check its work.
# `repo_map` is allowed again: it hung for 15+ minutes on a repository with build output (the 2026-10-13 run) because
# its ranking was quadratic and it walked ignored folders; it now lists files with git, ranks in one pass and has a budget.
BASE_TOOLS = ["read_file", "list_dir", "glob", "grep", "repo_map", "code_search", "git_diff", "git_status", "tool_output",
              "edit_file", "multi_edit", "write_file", "apply_patch", "todo_read"]
SHELL_TOOLS = ["run_shell", "shell_output", "shell_list", "shell_kill"]
TEST_TOOLS = ["run_tests"]       # stands in for a shell when there is none
SCRIPT_TOOLS = ["run_script"]    # the repo's generator scripts, allowlisted, only in the windows_job sandbox without network
SHELL_ALLOW = ["python -m pytest*", "python -m ruff*", "python -m py_compile*", "python -c *", "pytest*", "dir*", "type *", "echo *"]


class FactoryError(RuntimeError):
    pass


@dataclass
class FactoryTask:
    id: str
    title: str
    kind: str
    commit: str                       # the commit that solved it; the agent starts from its first parent
    prompt: str                       # written as an issue: the problem or the wish, never the solution
    subject: str = ""                 # the commit's subject line, to find it again after a history rewrite
    tests: list[str] = field(default_factory=list)         # pytest node ids (repo-relative) that must pass
    test_files: list[str] = field(default_factory=list)    # files laid over the worktree from the commit before judging
    setup_files: list[str] = field(default_factory=list)   # files taken from the commit and present when the agent starts
    checks: list[dict] = field(default_factory=list)       # {"file": rel, "regex": "..."} each must match after the run
    allowed_extra: list[str] = field(default_factory=list)  # extra paths the agent may touch without it counting as scope creep
    timeout_s: float = 900
    max_iterations: int = 60
    needs_shell: bool = False       # cannot be done without running a command (regenerating a file): skipped when the agent has no shell

    def problems(self) -> list[str]:
        out = []
        if self.kind not in KINDS:
            out.append(f"kind {self.kind!r} is not one of {KINDS}")
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.commit):
            out.append("commit must be a hex commit id")
        if not self.prompt.strip():
            out.append("no prompt")
        if not (self.tests or self.checks):
            out.append("no judge (tests or checks)")
        blob = json.dumps(self.__dict__)
        if DRIVE_PATH.search(blob):
            out.append("a machine path in the task data")
        for rel in [*self.tests, *self.test_files, *self.setup_files, *self.allowed_extra, *(c.get("file", "") for c in self.checks)]:
            path = rel.split("::")[0]
            if path.startswith(("/", "..")) or ".." in Path(path).parts:
                out.append(f"{rel!r} is not a repository-relative path")
        return out


def load_tasks(path: Path | str = DATA_FILE) -> list[FactoryTask]:
    """The task list. A task with a problem (no judge, a machine path ...) is an error, not a silently skipped row."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks, seen = [], set()
    for row in raw["tasks"] if isinstance(raw, dict) else raw:
        task = FactoryTask(**row)
        if task.id in seen:
            raise FactoryError(f"duplicate task id {task.id}")
        seen.add(task.id)
        if task.problems():
            raise FactoryError(f"task {task.id}: " + "; ".join(task.problems()))
        tasks.append(task)
    return tasks


# ----------------------------------------------------------------------------------------------------- git helpers

def git(repo: Path, *args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise FactoryError(f"git {' '.join(args)} failed: {(proc.stdout + proc.stderr)[-600:]}")
    return proc


def resolve_commit(repo: Path, task: FactoryTask) -> str:
    """The full id of the task's commit: by id, else (history rewritten) by the first commit with the same subject."""
    probe = git(repo, "rev-parse", "--verify", "--quiet", f"{task.commit}^{{commit}}", check=False)
    if probe.returncode == 0:
        return probe.stdout.strip()
    if task.subject:
        found = git(repo, "log", "--all", "--no-merges", "--fixed-strings", f"--grep={task.subject}", "--format=%H%x00%s", check=False)
        for line in found.stdout.splitlines():
            sha, _, subject = line.partition("\x00")
            if subject == task.subject:
                return sha
    raise FactoryError(f"task {task.id}: commit {task.commit} is not in this repository")


def file_at(repo: Path, commit: str, rel: str) -> Optional[bytes]:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{rel}"], capture_output=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def changed_in(repo: Path, commit: str) -> list[str]:
    """The files the real change touched."""
    return [x for x in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", commit).stdout.splitlines() if x]


def restore(ws: Path, commit: str, paths: list[str]) -> list[str]:
    """Put the commit's version of each path (that exists in it) into the worktree, through git so line endings match the
    checkout. Returns the paths it could not find in the commit."""
    have = [p for p in paths if file_at(ws, commit, p) is not None]
    if have:
        git(ws, "checkout", commit, "--", *have)
    return [p for p in paths if p not in have]


# ------------------------------------------------------------------------------------------------------ worktrees

class Bench:
    """One task's throwaway git worktree: made from the task's base commit, removed on exit. Only the worktree this object
    created is ever removed."""

    def __init__(self, repo: Path, task: FactoryTask, workdir: Optional[Path] = None):
        self.repo, self.task = Path(repo), task
        self.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir())
        self.commit = resolve_commit(self.repo, task)
        self.path = self.workdir / f"{PREFIX}{task.id}-{uuid.uuid4().hex[:6]}"
        self.created = False
        self.before: dict[str, str] = {}

    def __enter__(self) -> "Bench":
        self.workdir.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(self.path), f"{self.commit}^")
        self.created = True
        missing = restore(self.path, self.commit, self.task.setup_files)
        if missing:
            raise FactoryError(f"task {self.task.id}: setup file(s) {missing} not in the commit")
        git(self.path, "reset", "-q", check=False)      # setup files are changes against the base, not staged ones
        self.before = self.snapshot()
        return self

    def __exit__(self, *exc) -> None:
        if not self.created:
            return
        git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
        if self.path.exists():
            _discard(self.path)             # a handle still held (Windows): same retry as the eval workspaces
        git(self.repo, "worktree", "prune", check=False)

    def snapshot(self) -> dict[str, str]:
        """path -> content hash for every file that differs from the base commit (modified, added, deleted)."""
        out = git(self.path, "status", "--porcelain", "-uall", "-z", check=False).stdout
        snap: dict[str, str] = {}
        for entry in [e for e in out.split("\x00") if e]:
            rel = entry[3:].strip().strip('"')
            if " -> " in rel:
                rel = rel.split(" -> ")[-1]
            target = self.path / rel
            snap[rel] = hashlib.sha1(target.read_bytes()).hexdigest() if target.is_file() else "gone"
        return snap

    def changed(self) -> list[str]:
        """What the agent changed since it started (setup files it left alone do not count)."""
        now = self.snapshot()
        return sorted(rel for rel in set(now) | set(self.before) if now.get(rel, "same") != self.before.get(rel, "same"))


# ---------------------------------------------------------------------------------------------------------- judging

def _run(cmd: list[str], cwd: Path, timeout: float, env: Optional[dict] = None) -> tuple[int, str]:
    environ = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=environ)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {int(timeout)}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr)


def allowed_paths(repo: Path, task: FactoryTask, commit: str) -> set[str]:
    return set(changed_in(repo, commit)) | set(task.allowed_extra) | set(task.test_files)


def is_unrelated(rel: str, allowed: set[str]) -> bool:
    """A file the agent changed that the real change did not touch. New or edited tests and the changelog are not scope creep."""
    if rel in allowed or rel.startswith("tests/") or rel == "CHANGELOG.md":
        return False
    return not rel.endswith((".pyc",)) and "__pycache__" not in rel and ".pytest_cache" not in rel


def judge(bench: Bench, python: str, *, test_timeout: float = 600, lint: bool = True) -> dict:
    """Lay the commit's tests over the worktree, run them, run the text checks, ruff the touched Python files, find scope creep."""
    task, ws = bench.task, bench.path
    agent_changed = bench.changed()
    restore(ws, bench.commit, task.test_files)        # the commit's version wins over whatever the agent did to those files
    checks: list[Check] = []
    if task.tests:
        rc, out = _run([python, "-m", "pytest", *task.tests, "-q", "-x", "-p", "no:cacheprovider", "--no-header", "-p", "no:xdist"],
                       ws, test_timeout)
        tail = " | ".join([ln for ln in out.strip().splitlines() if ln.strip()][-6:])[:700]
        checks.append(Check("tests pass", rc == 0, "" if rc == 0 else f"exit {rc}: {tail}"))
    for spec in task.checks:
        target = ws / spec["file"]
        text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        ok = bool(re.search(spec["regex"], text, re.M | re.S))
        checks.append(Check(f"{spec['file']} has {spec.get('what') or spec['regex'][:40]}", ok, "" if ok else "no match"))
    pys = [r for r in agent_changed if r.endswith(".py") and (ws / r).is_file()]
    if lint and pys:
        rc, out = _run([python, "-m", "ruff", "check", "--no-cache", *pys], ws, 120)
        checks.append(Check("ruff clean", rc == 0, "" if rc == 0 else out.strip()[-400:]))
    allowed = allowed_paths(bench.repo, task, bench.commit)
    unrelated = sorted(r for r in agent_changed if is_unrelated(r, allowed))
    return {"checks": checks, "changed": agent_changed, "unrelated": unrelated}


def classify(*, passed: bool, error: Optional[str], stopped: str, edits: list[str], tool_errors: int, tool_calls: int,
             text_tool_call: bool, tests_ok: bool, lint_ok: bool, timed_out: bool) -> str:
    """One failure kind for a failed task (see the module doc); "" when it passed."""
    if passed:
        return ""
    err = error or ""
    if CONTEXT_PATTERN.search(err):
        return "context overflow"
    if timed_out or TIMEOUT_PATTERN.search(err) or TIMEOUT_PATTERN.search(stopped):
        return "timeout"
    if BAD_CALL_PATTERN.search(err):
        return "bad tool call"          # the server refused the model's own tool call (it could not parse the arguments)
    if err and LIMIT_PATTERN.search(err):
        return "provider error"
    broken_calls = text_tool_call or (tool_errors >= 2 and tool_errors * 2 >= max(1, tool_calls))
    if tests_ok and not lint_ok:
        return "lint"
    if tests_ok:
        return "scope"
    if not edits:
        return "bad tool call" if broken_calls else ("provider error" if err else "gave up")
    return "bad tool call" if broken_calls and tool_errors >= 3 else "wrong edit"


# --------------------------------------------------------------------------------------------------- the agent side

def small_model_overlay(mode: str) -> dict:
    """`native_agent.small_model` for a bench run. An endpoint given by --base-url is not named as a local engine, so
    small-model mode's "auto" never turns on for it: "on" (the default here) forces it, "off" leaves the full tool set and
    prompt, "auto" leaves the decision to ABP."""
    return {} if mode == "auto" else {"small_model": {"enabled": mode != "off"}}


def agent_settings(*, model: str, context: int = 0, timeout_s: float = 900, max_iterations: int = 60,
                   shell: str = "none", sandbox_paths: Optional[list[str]] = None, small_model: str = "on",
                   extra_tools: Optional[list[str]] = None, script_tool: bool = False) -> dict:
    """The `native_agent` overlay a bench run uses (the confined dev swarm's shape). `shell` is "none" (no shell tool at
    all) or "sandbox" (run_shell, inside the windows_job sandbox with no network; `sandbox_paths` readable). `extra_tools`
    (the conductor's read-only codeintel and vault-recall tools) are allowed too: offered but denied, they were dead weight."""
    from bot.agent_runtime import tools

    extra = set(extra_tools or []) | (set(SCRIPT_TOOLS) if script_tool else set())
    allowed = set(BASE_TOOLS) | (set(SHELL_TOOLS) if shell == "sandbox" else set(TEST_TOOLS)) | extra
    names = sorted({s["name"] for s in tools.all_tool_schemas()})
    rules = [{"decision": "deny", "tool": n, "note": "not a bench tool"} for n in names if n not in allowed]
    rules += [{"decision": "allow", "tool": n} for n in sorted(extra)]
    if shell == "sandbox":
        rules += [{"decision": "allow", "tool": "run_shell", "match": p} for p in SHELL_ALLOW]
    else:
        rules.append({"decision": "allow", "tool": "run_tests"})        # its arguments are validated to test paths in the workspace
    cfg: dict[str, Any] = {
        "permissions": {"mode": "accept_edits", "allow_bypass": False, "locked": True, "rules": rules},
        "limits": {"max_iterations": int(max_iterations), "max_seconds": int(timeout_s), "max_tokens": 0},
    }
    if context:
        cfg["context_windows"] = {model: int(context)}
    cfg.update(small_model_overlay(small_model))
    if shell == "sandbox" or script_tool:          # run_script refuses anything but a confining sandbox without network
        cfg["sandbox"] = {"backend": "windows_job", "network": "none",
                          "network_none": {"extra_paths": list(sandbox_paths or [])},
                          "windows_job": {"memory_mb": 6144, "active_process_limit": 128}}
    return cfg


@contextlib.contextmanager
def agent_environment(settings: dict, *, python: str = ""):
    """Config overlay and a PATH whose `python` is the judge's, for one agent run; both restored after."""
    from bot.agent_runtime import taint
    from bot.config import config

    from .runner import _merge

    saved_cfg, saved_path = config._data, os.environ.get("PATH", "")
    config._data = {**saved_cfg, "native_agent": _merge(saved_cfg.get("native_agent") or {}, settings)}
    if python:
        os.environ["PATH"] = str(Path(python).parent) + os.pathsep + saved_path
    taint.forget_all()
    try:
        yield
    finally:
        config._data = saved_cfg
        os.environ["PATH"] = saved_path
        taint.forget_all()


def endpoint_transport(base_url: str, api_key: str = ""):
    """A transport for any OpenAI-compatible endpoint (a ModelMistress instance, llama.cpp, vLLM, a provider)."""
    from bot.agent_runtime.transports import build_openai_transport

    return build_openai_transport(protocol="openai", base_url=base_url.rstrip("/"), api_key=api_key or "unused")


def drive_agent(prompt: str, ws: Path, transport: Any, model: str, *, timeout_s: float, max_iterations: int, context: int = 0,
                shell: str = "none", sandbox_paths: Optional[list[str]] = None, python: str = "", small_model: str = "on",
                extra_tools: Optional[list[str]] = None, script_tool: bool = False) -> Any:
    """ABP's native agent, headless (abp_run), on `ws`, with the bench's tool and sandbox settings. Returns the abp_run
    RunResult. A hard wall-clock guard stops a model call that never returns. `extra_tools` (the conductor's codeintel and
    vault-recall tools) are offered on top of the bench set, small-model mode included; the bench itself passes none."""
    from abp_run import core

    settings = agent_settings(model=model, context=context, timeout_s=timeout_s, max_iterations=max_iterations, shell=shell,
                              sandbox_paths=sandbox_paths, small_model=small_model, extra_tools=extra_tools,
                              script_tool=script_tool)
    allowed = frozenset(BASE_TOOLS) | (frozenset(SHELL_TOOLS) if shell == "sandbox" else frozenset(TEST_TOOLS))
    if small_model == "on":
        # A turn that names its tools (allowed_tools) is sent exactly those: small-model mode's own tool filter does not
        # apply to it. So the bench offers the tools both lists agree on, the set a small model would be sent.
        from bot.agent_runtime import small_model as small

        allowed = allowed & small.core_tools()
    if extra_tools:
        allowed = allowed | frozenset(extra_tools)
    if script_tool:
        allowed = allowed | frozenset(SCRIPT_TOOLS)
    root = Path(tempfile.mkdtemp(prefix="abp-bench-run-"))
    try:
        with core.ephemeral_environment(root, "deny"), agent_environment(settings, python=python):
            async def turn():
                from bot.agent_runtime import browser, code_intel

                try:
                    return await asyncio.wait_for(
                        core.run_turn(prompt, transport=transport, model=model, cwd=ws, permission_mode="accept_edits",
                                      timeout_s=timeout_s, extra_context={"allowed_tools": allowed}),
                        timeout=timeout_s + 60)
                finally:
                    await code_intel.shutdown_all()
                    await browser.shutdown_all()

            # Not asyncio.run(): its shutdown waits for the loop's worker threads, and a tool stuck in one (a repo scan
            # of a big tree) would hold the whole bench past its time limit. The loop is closed without waiting.
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(turn())
            except asyncio.TimeoutError:
                return core.RunResult(ok=False, error="timed out: the model call did not return", model=model,
                                      exit_code=core.EXIT_FAILED, stopped="time")
            finally:
                loop.close()
    finally:
        _discard(root)


def script_tool_supported() -> bool:
    """run_script only runs inside the windows_job sandbox without network; elsewhere a task that needs it stays skipped."""
    try:
        from bot.agent_runtime import win_job

        return bool(win_job.is_supported())
    except Exception:  # noqa: BLE001
        return False


KNOWLEDGE_SHARE = 0.03          # the knowledge may take at most this share of the model's context window


def knowledge_budget(context: int = 0, cap_chars: int = 1200) -> int:
    """Characters of knowledge a prompt may carry: `cap_chars`, and at most KNOWLEDGE_SHARE of the context (~4 chars/token)."""
    return min(cap_chars, int((context or 32768) * KNOWLEDGE_SHARE * 4))


def knowledge_block(task: FactoryTask, ws: Path, knowledge: str = "off", *, context: int = 0) -> str:
    """LF7: with `knowledge` "km", the project knowledge module (bot/conductor/projectkm.py, mmkm/1) built from the task's own
    base worktree (never from a later commit, so it cannot hold the answer) and sliced for the issue. Lessons from earlier
    Factory runs are left out: they come from this machine's history and could hold the real change."""
    if knowledge != "km":
        return ""
    from bot.conductor import projectkm

    with tempfile.TemporaryDirectory(prefix="abp-km-") as out:
        info = projectkm.build(ws, "abp", Path(out), lessons=False)
        chunks = projectkm.load(Path(info["memory"]))
    return projectkm.format_block(projectkm.select(chunks, f"{task.title}\n{task.prompt}", re.findall(r"[\w./-]+\.py\b", task.prompt),
                                                   budget_chars=knowledge_budget(context), strict=True))


def build_prompt(task: FactoryTask, *, shell: str = "none", knowledge: str = "", script_tool: bool = False) -> str:
    how = ("You can run `python -m pytest -q -p no:cacheprovider <path>` and `python -m ruff check <file>` in a sandbox with no network."
           if shell == "sandbox" else
           "You cannot run commands here, but the run_tests tool runs pytest on test files (for example tests/test_x.py::test_y) "
           "and reports what failed: read the code, make the change carefully, then check it with run_tests.")
    if script_tool:
        how += " The run_script tool runs the repository's own generator scripts (for example scripts/export_openapi.py) in the sandbox."
    # The knowledge goes BEFORE the issue, framed as reference: placed after it (and phrased as "follow these") it read as
    # extra instructions, and the model wandered through the modules it named (LF7 bench, 2026-10-26/27).
    return (f"You are working in a checkout of a Python repository (your current folder). Resolve the issue below by changing "
            f"the code in this folder. {how} Keep the change small and focused: touch only what the issue needs, delete nothing, "
            f"add or update a test when it is natural. Do not commit. Finish with a two-line summary of what you changed.\n\n" +
            (f"{knowledge.strip()}\n\n" if knowledge else "") +
            f"## Issue: {task.title}\n\n{task.prompt.strip()}\n")


def visible_tests(ws: Path, changed: list[str]) -> list[str]:
    """Test files the agent can see for its change: tests it wrote or edited, and the existing tests/test_<module>.py of each
    module it changed. Never the judge's hidden tests (those are laid over the worktree only when judging)."""
    out = [r for r in changed if r.endswith(".py") and (r.startswith("tests/") or Path(r).name.startswith("test_"))]
    for r in changed:
        if r.endswith(".py") and not r.startswith("tests/"):
            cand = f"tests/test_{Path(r).stem}.py"
            if (ws / cand).is_file():
                out.append(cand)
    return [r for r in dict.fromkeys(out) if (ws / r).is_file()][:5]


def self_check(bench: "Bench", python: str, *, test_timeout: float = 300) -> str:
    """After an agent turn: did the edits land, does every changed Python file parse and pass ruff, do the visible tests
    pass? Returns the problems as feedback for another turn, or "" when there are none."""
    ws = bench.path
    changed = bench.changed()
    if not changed:
        return ("No file in the checkout changed: your edits did not apply, or you made none. Make the change with edit_file / "
                "write_file and read the edited lines back to confirm them.")
    problems: list[str] = []
    pys = [r for r in changed if r.endswith(".py") and (ws / r).is_file()]
    for r in pys:                          # parsed here, not with py_compile: that writes __pycache__ into the worktree
        try:
            compile((ws / r).read_text(encoding="utf-8", errors="replace"), r, "exec", dont_inherit=True)
        except SyntaxError as exc:
            problems.append(f"{r} does not parse: {exc.msg} at line {exc.lineno}")
    if pys:
        rc, out = _run([python, "-m", "ruff", "check", "--no-cache", "--output-format", "concise", *pys], ws, 120)
        if rc != 0:
            problems.append("ruff reports (fix these; the change is rejected while any remain):\n" + out.strip()[-1500:])
    tests = visible_tests(ws, changed)
    if tests and not problems:
        rc, out = _run([python, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider", "--no-header", "-p", "no:xdist",
                        "--tb=short"], ws, test_timeout)
        if rc not in (0, 5):
            problems.append(f"pytest {' '.join(tests)} fails:\n" + out.strip()[-2000:])
    return "\n\n".join(problems)


def fix_prompt(task: FactoryTask, base_prompt: str, feedback: str, changed: list[str]) -> str:
    return (base_prompt + f"\n## Check of your change so far\n\nYou already changed: {', '.join(changed) or 'nothing'}. "
            f"An automatic check of that change found these problems:\n\n{feedback}\n\nFix exactly these problems and keep the "
            f"rest of your change. Read the lines you edit again before and after editing them.\n")


def _merge_results(results: list) -> dict:
    calls = [c for r in results for c in (r.tool_calls or [])]
    last = results[-1]
    return {"iterations": sum(r.iterations or 0 for r in results), "tokens": sum(r.tokens or 0 for r in results),
            "denied": sum(r.denied or 0 for r in results), "run_id": last.run_id or None, "calls": calls,
            "stopped": last.stopped or "", "error": last.error or None, "reply": last.reply or ""}


# ------------------------------------------------------------------------------------------------------ run + report

def run_task(task: FactoryTask, transport_factory: Callable[[FactoryTask], Any], *, model: str, python: str,
             repo: Path = REPO, workdir: Optional[Path] = None, context: int = 0, shell: str = "none",
             sandbox_paths: Optional[list[str]] = None, timeout_s: Optional[float] = None, keep: bool = False,
             lint: bool = True, test_timeout: float = 300, small_model: str = "on", knowledge: str = "off",
             fix_rounds: int = 2, script_tool: bool = True) -> dict:
    """One task end to end: worktree, agent, judge, cleanup. Always returns a result row (a crash is a failed row).

    After the agent's turn a self-check (edits landed, changed files compile and pass ruff, the visible tests pass) runs; its
    problems go back to the agent for up to `fix_rounds` more turns within the task's time limit. A task that needs a command
    gets the run_script tool (generator scripts only, windows_job sandbox, no network) when `script_tool` is on."""
    limit = float(timeout_s or task.timeout_s)
    started = time.monotonic()
    row: dict[str, Any] = {"id": task.id, "title": task.title, "category": task.kind, "commit": task.commit, "passed": False,
                           "checks": [], "error": None, "duration_ms": 0, "iterations": 0, "tokens": 0, "tool_counts": {},
                           "denied": 0, "run_id": None, "limited": False, "retry_at": None, "workspace": None,
                           "tool_errors": 0, "tool_error_counts": {}, "tool_calls": 0, "changed": [], "unrelated": [], "clean": False,
                           "failure_kind": "", "text_tool_call": False, "stopped": ""}
    scripted = False
    if task.needs_shell and shell != "sandbox":
        scripted = script_tool and script_tool_supported()
        if not scripted:
            row.update(error="not run: this task needs a command (regenerating a file) and the agent has neither a shell nor "
                             "the confined run_script tool", limited=True, failure_kind="", duration_ms=0)
            return row
    bench: Optional[Bench] = None
    try:
        bench = Bench(repo, task, workdir)
        bench.__enter__()
        kb = knowledge_block(task, bench.path, knowledge, context=context)
        row["knowledge_chars"] = len(kb)
        prompt = build_prompt(task, shell=shell, knowledge=kb, script_tool=scripted)
        results, feedback_log = [], []
        current = prompt
        while True:
            left = limit - (time.monotonic() - started)
            results.append(drive_agent(current, bench.path, transport_factory(task), model, timeout_s=limit if not results else left,
                                       max_iterations=task.max_iterations, context=context, shell=shell, sandbox_paths=sandbox_paths,
                                       python=python, small_model=small_model, script_tool=scripted))
            if len(feedback_log) >= fix_rounds or limit - (time.monotonic() - started) < min(90.0, 0.15 * limit) or results[-1].error:
                break
            fb = self_check(bench, python, test_timeout=min(test_timeout, 180))
            if not fb:
                break
            feedback_log.append(fb[:3000])
            current = fix_prompt(task, prompt, fb, bench.changed())
        merged = _merge_results(results)
        row["fix_rounds"], row["self_check"] = len(feedback_log), [f.splitlines()[0][:200] for f in feedback_log]
        row["duration_ms"] = int((time.monotonic() - started) * 1000)
        calls = merged["calls"]
        counts: dict[str, int] = {}
        for call in calls:
            counts[call.get("tool")] = counts.get(call.get("tool"), 0) + 1
        row.update(iterations=merged["iterations"], tokens=merged["tokens"], denied=merged["denied"], run_id=merged["run_id"],
                   tool_counts=dict(sorted(counts.items())), tool_calls=len(calls),
                   tool_errors=sum(1 for c in calls if c.get("status") == "failed"),
                   tool_error_counts=dict(sorted(Counter(c.get("tool") for c in calls if c.get("status") == "failed").items())),
                   stopped=merged["stopped"], error=merged["error"],
                   text_tool_call=bool(TEXT_TOOL_CALL.search(merged["reply"])))
        result = results[-1]
        verdict = judge(bench, python, test_timeout=test_timeout, lint=lint)
        checks = verdict["checks"]
        row["checks"] = [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]
        row["changed"], row["unrelated"] = verdict["changed"], verdict["unrelated"]
        tests_ok = all(c.ok for c in checks if not c.name.startswith("ruff"))
        lint_ok = all(c.ok for c in checks if c.name.startswith("ruff"))
        row["passed"] = bool(checks) and tests_ok and lint_ok
        row["clean"] = row["passed"] and not row["unrelated"]
        edits = [c for c in verdict["changed"] if not c.startswith("tests/")] or verdict["changed"]
        timed_out = bool(result.stopped and TIMEOUT_PATTERN.search(result.stopped)) or row["duration_ms"] / 1000 >= limit
        row["failure_kind"] = classify(passed=row["passed"], error=row["error"], stopped=row["stopped"], edits=edits,
                                       tool_errors=row["tool_errors"], tool_calls=row["tool_calls"],
                                       text_tool_call=row["text_tool_call"], tests_ok=tests_ok, lint_ok=lint_ok, timed_out=timed_out)
        if row["passed"] and row["unrelated"]:
            row["failure_kind"] = ""            # passed; the scope note stays in `unrelated` and in the clean rate
        if row["failure_kind"] == "provider error":
            row["limited"] = True               # the model was not measured
        if keep:
            row["workspace"] = str(bench.path)
    except Exception as exc:  # noqa: BLE001 - a crash in one task is a result, not the end of the run
        row["error"] = f"{type(exc).__name__}: {exc}"[:600]
        row["failure_kind"] = "provider error" if LIMIT_PATTERN.search(row["error"]) else "gave up"
    finally:
        row["duration_ms"] = row["duration_ms"] or int((time.monotonic() - started) * 1000)
        if bench is not None and not keep:
            bench.__exit__(None, None, None)
    return row


def summarize_rows(rows: list[dict]) -> dict:
    measured = [r for r in rows if not r.get("limited")]
    kinds: dict[str, int] = {}
    for r in measured:
        if r.get("failure_kind"):
            kinds[r["failure_kind"]] = kinds.get(r["failure_kind"], 0) + 1
    n = len(measured)
    return {
        "total": len(rows), "measured": n, "limited": len(rows) - n,
        "passed": sum(1 for r in measured if r["passed"]),
        "clean": sum(1 for r in measured if r.get("clean")),
        "score": round(100.0 * sum(1 for r in measured if r["passed"]) / n, 1) if n else 0.0,
        "tokens": sum(r["tokens"] for r in rows),
        "avg_seconds": round(sum(r["duration_ms"] for r in measured) / n / 1000, 1) if n else 0.0,
        "avg_tokens": round(sum(r["tokens"] for r in measured) / n) if n else 0,
        "tool_errors": sum(r.get("tool_errors", 0) for r in measured),
        "tool_calls": sum(r.get("tool_calls", 0) for r in measured),
        "text_tool_calls": sum(1 for r in measured if r.get("text_tool_call")),
        "failure_kinds": dict(sorted(kinds.items())),
        "by_kind": {k: f"{sum(1 for r in measured if r['category'] == k and r['passed'])}/{sum(1 for r in measured if r['category'] == k)}"
                    for k in sorted({r["category"] for r in measured})},
    }


def run_all(tasks: list[FactoryTask], transport_factory: Callable[[FactoryTask], Any], *, model: str, python: str,
            log: Callable[[str], None] = print, **kw) -> dict:
    rows = []
    for i, task in enumerate(tasks, 1):
        log(f"[{i}/{len(tasks)}] {task.id} ...")
        row = run_task(task, transport_factory, model=model, python=python, **kw)
        rows.append(row)
        log(f"    {'PASS' if row['passed'] else 'FAIL'} {row['duration_ms'] / 1000:.0f}s {row['tokens']} tokens "
            f"{row['tool_errors']}/{row['tool_calls']} tool errors {row['failure_kind']}")
    summary = summarize_rows(rows)
    return {"schema": SCHEMA_VERSION, "mode": "factory", "model": model, "when": time.time(), **summary,
            "duration_ms": sum(r["duration_ms"] for r in rows),
            "notes": ["each task runs in a throwaway git worktree of the commit's parent", f"shell tool: {kw.get('shell', 'none')}",
                      f"small-model mode: {kw.get('small_model', 'on')}", f"project knowledge (LF7): {kw.get('knowledge', 'off')}", f"self-check fix rounds: {kw.get('fix_rounds', 2)}",
                      f"run_script tool for command tasks: {'on' if kw.get('script_tool', True) else 'off'}",
                      f"context window told to the agent: {kw.get('context') or 'default'}"],
            "results": rows}


def render(report: dict) -> str:
    lines = [f"factory bench - model {report['model']}",
             f"passed {report['passed']}/{report['measured']} ({report['score']}%), clean {report['clean']}  "
             f"avg {report['avg_seconds']} s  avg {report['avg_tokens']} tokens  tool errors {report['tool_errors']}/{report['tool_calls']}"]
    if report.get("limited"):
        lines.append(f"{report['limited']} task(s) did not measure the model (the endpoint answered an error)")
    if report["failure_kinds"]:
        lines.append("failures: " + ", ".join(f"{k} {v}" for k, v in report["failure_kinds"].items()))
    if report.get("text_tool_calls"):
        lines.append(f"{report['text_tool_calls']} run(s) ended with a tool call written as text (no parser for it yet)")
    lines.append("by kind: " + ", ".join(f"{k} {v}" for k, v in report["by_kind"].items()))
    lines.append("")
    for r in report["results"]:
        mark = "PASS" if r["passed"] else ("SKIP" if r.get("limited") else "FAIL")
        extra = f"  [{r['failure_kind']}]" if r.get("failure_kind") else (f"  [touched unrelated: {', '.join(r['unrelated'])}]" if r.get("unrelated") else "")
        lines.append(f"  {mark}  {r['id']:<30} {r['duration_ms'] / 1000:>5.0f}s {r['tokens']:>7} tokens  {r['commit'][:8]}{extra}")
        if not r["passed"] and r.get("error"):
            lines.append(f"        error: {r['error'][:200]}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------ verification

def verify_task(task: FactoryTask, *, python: str, repo: Path = REPO, workdir: Optional[Path] = None,
                test_timeout: float = 600) -> dict:
    """The task is sound: its judge fails on the untouched base, and passes once the real change is applied. (A judge that
    passes on the base measures nothing; one that fails on the real fix cannot be earned.)"""
    out = {"id": task.id, "base_fails": None, "fix_passes": None, "problems": []}
    with Bench(repo, task, workdir) as bench:
        base = judge(bench, python, test_timeout=test_timeout, lint=False)
        out["base_fails"] = not all(c.ok for c in base["checks"])
        restore(bench.path, bench.commit, changed_in(repo, bench.commit))      # the real change, applied
        fixed = judge(bench, python, test_timeout=test_timeout, lint=False)
        out["fix_passes"] = all(c.ok for c in fixed["checks"])
        out["detail"] = [c.detail for c in (base["checks"] if not out["base_fails"] else fixed["checks"]) if c.detail and not c.ok][:2]
    if not out["base_fails"]:
        out["problems"].append("the judge already passes on the base commit")
    if not out["fix_passes"]:
        out["problems"].append("the judge fails with the real change applied")
    return out


def main(args, python: str) -> int:
    """`python -m abp_agenteval factory ...` (see __main__)."""
    repo = Path(args.repo) if args.repo else REPO
    tasks = load_tasks(args.tasks) if args.tasks else load_tasks()
    if args.task:
        unknown = set(args.task) - {t.id for t in tasks}
        if unknown:
            print(f"unknown task(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        tasks = [t for t in tasks if t.id in set(args.task)]
    workdir = Path(args.workdir) if args.workdir else None
    if args.factory_cmd == "list":
        for t in tasks:
            print(f"{t.id:<32} {t.kind:<9} {t.commit[:8]}  {t.title}")
        return 0
    if args.factory_cmd == "verify":
        bad = 0
        for t in tasks:
            res = verify_task(t, python=python, repo=repo, workdir=workdir)
            bad += bool(res["problems"])
            print(f"{'ok  ' if not res['problems'] else 'BAD '} {t.id:<32} base fails={res['base_fails']} fix passes={res['fix_passes']} "
                  f"{'; '.join(res['problems'])} {res.get('detail') or ''}", flush=True)
        return 1 if bad else 0
    # run
    if not args.model:
        print("factory run needs --model", file=sys.stderr)
        return 2
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    transport = endpoint_transport(args.base_url, api_key)
    report = run_all(tasks, lambda _t: transport, model=args.model, python=python, repo=repo, workdir=workdir, context=args.context,
                     shell=args.shell, sandbox_paths=args.sandbox_path or [], timeout_s=args.timeout, keep=args.keep,
                     small_model=args.small_model, knowledge=getattr(args, "knowledge", "off"),
                     fix_rounds=getattr(args, "fix_rounds", 2), script_tool=getattr(args, "script_tool", "on") == "on")
    report["model"] = args.label or args.model
    if args.out:
        from . import report as rep

        rep.save(args.out, report)
    print(render(report))
    return 0 if report["passed"] == report["measured"] else 1
