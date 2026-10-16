"""The integrator: green task branches -> an integration branch, the full suite, and the push queue.

    integ = Integrator(repo, base="main", suite=[...])
    result = integ.integrate(["feat/a", "feat/b"], plan_id="...")

1. A new branch `integ/agents-N` (N = one past the highest there is) in its own worktree, from `base`.
2. Each task branch is merged in. A branch that does not merge cleanly is left out (merge aborted) and listed as a conflict.
3. The suite command runs in the worktree. Its output is saved to a log file next to the worktree.
4. Failed tests are re-run alone (the pytest `-rf` summary names them). A test that passes on the re-run is a **flake**; one that
   fails again is a **failure**. Both are recorded. Any failure makes the integration `red` and nothing is queued.
5. A green integration (flakes allowed, and listed) goes into the push queue as `awaiting_approval`.

Nothing here pushes, deploys or touches the base branch: the integration branch only exists locally until an approval (owner, or
Claude when the owner delegates) lets someone push it (`abp conductor approve`).
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from bot.conductor import store

FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s.*)?$", re.MULTILINE)
ERROR_LINE = re.compile(r"^ERROR\s+(\S+?)(?:\s+-\s.*)?$", re.MULTILINE)
TAIL = 4000


@dataclass
class Integration:
    id: str
    branch: str
    base: str
    status: str = "running"            # running | green | red | conflicts | empty
    plan_id: str = ""
    worktree: str = ""
    head_sha: str = ""
    merged: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    suite: list[str] = field(default_factory=list)
    suite_rc: Optional[int] = None
    flaky: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    tests_run: str = ""
    log: str = ""
    tail: str = ""
    note: str = ""
    created: float = 0.0
    queue_id: str = ""
    tasks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "user.name=conductor", "-c", "user.email=conductor@localhost.invalid",
                           "-c", "core.autocrlf=false", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def failed_tests(output: str) -> list[str]:
    """The test ids pytest's `-rf` summary names (failures and collection errors), in order, once each."""
    ids = FAILED_LINE.findall(output) + ERROR_LINE.findall(output)
    return list(dict.fromkeys(ids))


class Integrator:
    def __init__(self, repo: Any, *, base: str = "main", suite: Optional[Sequence[str]] = None, rerun: Optional[Sequence[str]] = None,
                 workdir: Optional[Path] = None, suite_timeout_s: float = 3600, prefix: str = "integ/agents-"):
        self.repo = Path(repo)
        self.base = base
        self.suite = list(suite) if suite else [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rf"]
        # A re-run of just the failed tests: the suite's own pytest prefix, or an explicit command ending where the ids go.
        self.rerun = list(rerun) if rerun else [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"]
        self.workdir = Path(workdir) if workdir else None
        self.timeout = suite_timeout_s
        self.prefix = prefix

    # ----------------------------------------------------------- branches
    def next_branch(self) -> str:
        names = _git(self.repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{self.prefix}*").stdout.split()
        nums = [int(m.group(1)) for n in names if (m := re.fullmatch(re.escape(self.prefix) + r"(\d+)", n))]
        return f"{self.prefix}{max(nums, default=0) + 1}"

    def _workdir(self) -> Path:
        if self.workdir:
            self.workdir.mkdir(parents=True, exist_ok=True)
            return self.workdir
        from bot.agent_runtime.state import state_dir

        return state_dir("conductor", "integ")

    def _run(self, cmd: Sequence[str], cwd: Path) -> tuple[int, str]:
        try:
            proc = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout)
            return proc.returncode, (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            return 124, out + f"\n[timed out after {self.timeout:.0f}s]"
        except OSError as exc:
            return 127, f"could not run {cmd[0]}: {exc}"

    # ---------------------------------------------------------- integrate
    def integrate(self, branches: Sequence[str], *, plan_id: str = "", tasks: Sequence[str] = (), persist: bool = True) -> Integration:
        name = self.next_branch()
        rec = Integration(id=uuid.uuid4().hex[:12], branch=name, base=self.base, plan_id=plan_id, suite=list(self.suite),
                          created=time.time(), tasks=list(tasks))
        wt = self._workdir() / name.replace("/", "-")
        _git(self.repo, "worktree", "add", "-q", "-b", name, str(wt), self.base)
        rec.worktree = str(wt)
        try:
            for b in branches:
                merge = _git(wt, "merge", "--no-ff", "--no-edit", "-m", f"Merge {b} into {name}", b, check=False)
                if merge.returncode == 0:
                    rec.merged.append(b)
                else:
                    files = _git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                    _git(wt, "merge", "--abort", check=False)
                    rec.conflicts.append({"branch": b, "files": files[:20], "message": (merge.stdout or merge.stderr).strip()[-300:]})
            rec.head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
            if not rec.merged:
                rec.status, rec.note = ("conflicts" if rec.conflicts else "empty"), "no branch could be merged"
            else:
                self._test(rec, wt)
        except Exception as exc:  # noqa: BLE001 - record what happened; the worktree stays for a look
            rec.status, rec.note = "red", f"integration error: {type(exc).__name__}: {exc}"[:300]
        if rec.status == "green" and rec.conflicts:
            rec.note = f"{len(rec.conflicts)} branch(es) left out: they did not merge cleanly"
        if persist:
            store.save_integration(rec.to_dict())
            if rec.status == "green":
                entry = store.enqueue(rec.to_dict())
                rec.queue_id = entry.get("id", "")
                store.save_integration(rec.to_dict())
        return rec

    def _test(self, rec: Integration, wt: Path) -> None:
        log = wt.parent / f"{wt.name}.suite.log"
        rc, out = self._run(self.suite, wt)
        rec.suite_rc = rc
        log.write_text(out, encoding="utf-8", errors="replace")
        rec.log = str(log)
        rec.tail = out[-TAIL:]
        m = re.search(r"^=+ (.*?(?:passed|failed|error).*?) =+$", out, re.MULTILINE) or re.search(r"(\d+ (?:passed|failed).*)", out)
        rec.tests_run = m.group(1).strip() if m else ""
        if rc == 0:
            rec.status = "green"
            return
        failing = failed_tests(out)
        if not failing:                                                # a crash or a timeout: nothing to re-run
            rec.status, rec.note = "red", f"the suite exited {rc} with no failed test named"
            return
        rc2, out2 = self._run([*self.rerun, *failing], wt)
        again = set(failed_tests(out2)) if rc2 != 0 else set()
        if rc2 != 0 and not again:
            again = set(failing)                                       # the re-run itself broke: treat all as failing
        rec.failed = [t for t in failing if t in again]
        rec.flaky = [t for t in failing if t not in again]
        (wt.parent / f"{wt.name}.rerun.log").write_text(out2, encoding="utf-8", errors="replace")
        if rec.failed:
            rec.status, rec.note = "red", f"{len(rec.failed)} test(s) fail again on a re-run"
            rec.tail = out2[-TAIL:]
        else:
            rec.status, rec.note = "green", f"{len(rec.flaky)} flaky test(s) passed on a re-run"


def push_command(entry: dict, remote: str = "origin") -> str:
    """What an approver runs to push an approved entry (shown, never executed, by the conductor)."""
    return f"git push {shlex.quote(remote)} {shlex.quote(entry['branch'])}"
