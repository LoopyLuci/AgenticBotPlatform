"""The integrator: green task branches -> an integration branch, the full suite, and the push queue.

    integ = Integrator(repo, base="main", suite=[...])
    result = integ.integrate(["feat/a", "feat/b"], plan_id="...")

0. When routes changed, the OpenAPI document and the SDK are regenerated (scripts/export_openapi.py) and committed on the branch.
1. A new branch `integ/agents-N` (N = one past the highest there is) in its own worktree, from `base`.
2. Each task branch is merged in. A branch that does not merge cleanly is left out (merge aborted) and listed as a conflict.
3. The suite command runs in the worktree, under the machine-wide suite lock, with pytest `-n` sized from the free CPUs and RAM,
   optionally split by test file into shards that run one after another (each shard's result is recorded). Its output is saved to
   a log file next to the worktree. A configured final gate (e.g. the full local pipeline without deploy) runs after a green suite.
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
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from bot.conductor import cache as cachelib
from bot.conductor import shards as shardlib
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
    repo: str = ""
    sdk: dict = field(default_factory=dict)         # {"checked", "changed", "files", "note"} - SDK/OpenAPI regeneration
    shards: list[dict] = field(default_factory=list)  # one result per suite shard
    budget: dict = field(default_factory=dict)      # the CPU/RAM budget the suite ran with
    gate: dict = field(default_factory=dict)        # the final gate command and its result, when one is configured
    selective: dict = field(default_factory=dict)   # the tests the merged diff affects, run before the full suite: {tests, rc, ...}
    cache: dict = field(default_factory=dict)       # {"hits": n, "stores": n} for suite/shard results

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
                 workdir: Optional[Path] = None, suite_timeout_s: float = 3600, prefix: str = "integ/agents-",
                 regen_sdk: bool = True, shards: int = 1, workers: Any = None, max_workers: int = 8,
                 lock_wait_s: Optional[float] = None, lock_path: Optional[Path] = None, gate: Optional[Sequence[str]] = None,
                 gate_timeout_s: float = 7200, selective_first: Optional[bool] = None, cache: Optional[cachelib.Cache] = None):
        """`workers`: None leaves the suite's own `-n`, "auto" sizes it from free CPU/RAM (at most max_workers), a number sets it.
        `lock_wait_s`: None = no machine lock; a number = hold the machine-wide suite lock, waiting that long for it."""
        self.repo = Path(repo)
        self.base = base
        self.suite = list(suite) if suite else [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rf"]
        # A re-run of just the failed tests: the suite's own pytest prefix, or an explicit command ending where the ids go.
        self.rerun = list(rerun) if rerun else [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"]
        self.workdir = Path(workdir) if workdir else None
        self.timeout = suite_timeout_s
        self.prefix = prefix
        self.regen_sdk = regen_sdk
        self.shards = max(1, int(shards or 1))
        self.workers = workers
        self.max_workers = int(max_workers)
        self.lock_wait_s = lock_wait_s
        self.lock_path = Path(lock_path) if lock_path else None
        self.gate = list(gate) if gate else []
        self.gate_timeout = gate_timeout_s
        cfg = None
        if selective_first is None or cache is None:
            from bot import conductor

            cfg = conductor.settings()
        self.selective_first = bool(cfg["integrator"]["selective_first"]) if selective_first is None else bool(selective_first)
        self.cache = cache if cache is not None else cachelib.from_settings(cfg)

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

    def _run(self, cmd: Sequence[str], cwd: Path, timeout: Optional[float] = None) -> tuple[int, str]:
        timeout = timeout or self.timeout
        try:
            proc = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
            return proc.returncode, (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            return 124, out + f"\n[timed out after {timeout:.0f}s]"
        except OSError as exc:
            return 127, f"could not run {cmd[0]}: {exc}"

    # ---------------------------------------------------------- integrate
    def integrate(self, branches: Sequence[str], *, plan_id: str = "", tasks: Sequence[str] = (), persist: bool = True) -> Integration:
        name = self.next_branch()
        rec = Integration(id=uuid.uuid4().hex[:12], branch=name, base=self.base, plan_id=plan_id, suite=list(self.suite),
                          created=time.time(), tasks=list(tasks), repo=str(self.repo))
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
                self._regen(rec, wt)
                if rec.status == "running":
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

    # ------------------------------------------------------------- the SDK
    ROUTE_LINE = re.compile(r"^[+-]\s*@\w+\.(?:get|post|put|delete|patch|api_route)\(|^[+-].*\binclude_router\(")

    def _routes_changed(self, wt: Path) -> bool:
        names = _git(wt, "diff", "--name-only", self.base, "HEAD", check=False).stdout.split()
        if any(n.startswith("bot/dashboard/") for n in names):
            return True
        if not any(n.endswith(".py") and n.startswith("bot/") for n in names):
            return False
        diff = _git(wt, "diff", "-U0", self.base, "HEAD", "--", "bot", check=False).stdout
        return any(self.ROUTE_LINE.match(line) for line in diff.splitlines())

    def _regen(self, rec: Integration, wt: Path) -> None:
        """Routes changed -> regenerate docs/api/openapi.json and sdk/typescript and commit that on the integration branch."""
        script = wt / "scripts" / "export_openapi.py"
        rec.sdk = {"checked": False, "changed": False, "files": 0, "note": ""}
        if not self.regen_sdk or not script.is_file():
            rec.sdk["note"] = "off" if not self.regen_sdk else "no scripts/export_openapi.py in this repository"
            return
        if not self._routes_changed(wt):
            rec.sdk.update(checked=True, note="no route changed")
            return
        rc, out = self._run([sys.executable, str(script)], wt, timeout=600)
        rec.sdk["checked"] = True
        if rc != 0:
            rec.status, rec.note, rec.tail = "red", f"SDK regeneration failed (exit {rc})", out[-TAIL:]
            rec.sdk["note"] = rec.note
            return
        paths = [p for p in ("docs/api", "sdk") if (wt / p).exists()]
        dirty = [ln[3:] for ln in _git(wt, "status", "--porcelain", "--", *paths).stdout.splitlines() if ln.strip()]
        if dirty:
            _git(wt, "add", "--", *paths)
            _git(wt, "commit", "-q", "-m", f"Regenerate the OpenAPI document and SDK ({rec.branch})\n\nconductor: routes changed on the merged branches")
            rec.head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
        rec.sdk.update(changed=bool(dirty), files=len(dirty), note=f"regenerated, {len(dirty)} file(s) committed" if dirty else "regenerated, up to date")

    # ------------------------------------------------------------ the suite
    def _lock(self):
        if self.lock_wait_s is None:
            return nullcontext()
        return shardlib.machine_lock(self.lock_wait_s, self.lock_path)

    def _commands(self, rec: Integration, wt: Path) -> list[tuple[str, list[str], int]]:
        """[(name, command, files)] - the suite as one run, or one command per shard; `-n` sized from the budget when asked."""
        suite = list(self.suite)
        if self.workers is not None:
            budget = (shardlib.Budget.measure(self.max_workers) if str(self.workers).lower() == "auto"
                      else shardlib.Budget(max(1, int(self.workers)), 0, 0.0, 0.0, "configured"))
            rec.budget = budget.to_dict()
            suite = shardlib.with_workers(suite, budget.workers)
        groups = shardlib.split_files(shardlib.find_test_files(wt), self.shards) if self.shards > 1 else []
        if len(groups) > 1:
            return [(f"shard {i}/{len(groups)}", cmd, len(g)) for i, (g, cmd) in enumerate(zip(groups, shardlib.shard_commands(suite, groups)), 1)]
        return [("all", suite, 0)]

    @staticmethod
    def _summary(out: str) -> str:
        m = re.search(r"^=+ (.*?(?:passed|failed|error).*?) =+$", out, re.MULTILINE) or re.search(r"(\d+ (?:passed|failed).*)", out)
        return m.group(1).strip() if m else ""

    def _test(self, rec: Integration, wt: Path) -> None:
        try:
            with self._lock():
                self._test_locked(rec, wt)
        except shardlib.MachineBusy as exc:
            rec.status, rec.note = "red", f"the machine is busy with another run: {exc}"[:300]

    def related_test_files(self, wt: Path) -> tuple[list[str], str]:
        """(test files, why-not) the diff base..HEAD affects, from codeintel's impact on the changed lines."""
        diff = _git(wt, "diff", "-U0", "--no-color", "--no-ext-diff", self.base, "HEAD", check=False).stdout
        if not diff.strip():
            return [], "no diff"
        try:
            from bot.codeintel import tools

            data = tools.impact(wt, diff, max_tokens=4000).data or {}
        except Exception as exc:  # noqa: BLE001 - no index: just run the full suite
            return [], f"impact unavailable ({type(exc).__name__})"
        tests = [t for t in data.get("tests", []) if (wt / t).is_file()]
        return tests, "" if tests else "no related tests found"

    def _selective(self, rec: Integration, wt: Path, tree: str) -> None:
        """Run the tests the merge affects first; recorded in rec.selective. The full suite still runs afterwards as the final gate."""
        tests, why = self.related_test_files(wt)
        rec.selective = {"tests": tests, "rc": None, "failed": [], "seconds": 0.0, "note": why}
        if not tests:
            return
        cmd = [*self.rerun, *tests]
        t0 = time.time()
        hit = self.cache.lookup(tree, cmd, tests)
        if hit:
            rc, out = 0, ""
            rec.cache["hits"] += 1
        else:
            rc, out = self._run(cmd, wt)
            if rc == 0 and self.cache.store(tree, cmd, tests, {"rc": 0}):
                rec.cache["stores"] += 1
        rec.selective.update(rc=rc, failed=failed_tests(out), seconds=round(time.time() - t0, 1), cached=bool(hit),
                             tests_run=self._summary(out), note=f"{len(tests)} related test file(s)")

    def _test_locked(self, rec: Integration, wt: Path) -> None:
        log = wt.parent / f"{wt.name}.suite.log"
        plan = self._commands(rec, wt)
        outs, worst, crashed, failing = [], 0, [], []
        tree = cachelib.tree_hash(wt) if self.cache.enabled else ""
        rec.cache = {"hits": 0, "stores": 0}
        if self.selective_first:
            self._selective(rec, wt, tree)
        for name, cmd, nfiles in plan:
            t0 = time.time()
            hit = self.cache.lookup(tree, cmd)
            if hit:
                rc, out = 0, str(hit.get("summary") or "[cached pass]")
                rec.cache["hits"] += 1
            else:
                rc, out = self._run(cmd, wt)
                if rc == 0 and self.cache.store(tree, cmd, (), {"rc": 0, "summary": f"[cached pass] {self._summary(out)}"}):
                    rec.cache["stores"] += 1
            named = failed_tests(out)
            rec.shards.append({"name": name, "files": nfiles, "rc": rc, "tests_run": self._summary(out), "failed": named,
                               "seconds": round(time.time() - t0, 1), "cached": bool(hit)})
            outs.append(out if len(plan) == 1 else f"===== {name} (exit {rc}) =====\n{out}")
            worst = worst or rc
            if rc != 0 and not named:
                crashed.append(f"{name} exited {rc}")
            failing += [t for t in named if t not in failing]
        out = "\n".join(outs)
        rec.suite_rc = worst
        log.write_text(out, encoding="utf-8", errors="replace")
        rec.log = str(log)
        rec.tail = out[-TAIL:]
        rec.tests_run = "; ".join(s["tests_run"] for s in rec.shards if s["tests_run"]) if len(plan) > 1 else rec.shards[0]["tests_run"]
        if worst == 0:
            rec.status = "green"
        elif crashed:                                                  # a crash or a timeout: nothing to re-run
            rec.status, rec.note = "red", f"the suite exited with no failed test named ({'; '.join(crashed)})"
            return
        else:
            rc2, out2 = self._run([*self.rerun, *failing], wt)
            again = set(failed_tests(out2)) if rc2 != 0 else set()
            if rc2 != 0 and not again:
                again = set(failing)                                   # the re-run itself broke: treat all as failing
            rec.failed = [t for t in failing if t in again]
            rec.flaky = [t for t in failing if t not in again]
            (wt.parent / f"{wt.name}.rerun.log").write_text(out2, encoding="utf-8", errors="replace")
            if rec.failed:
                rec.status, rec.note = "red", f"{len(rec.failed)} test(s) fail again on a re-run"
                rec.tail = out2[-TAIL:]
                return
            rec.status, rec.note = "green", f"{len(rec.flaky)} flaky test(s) passed on a re-run"
        if self.gate:                                                  # the final gate, e.g. the whole local pipeline without deploy
            rcg, outg = self._run(self.gate, wt, timeout=self.gate_timeout)
            rec.gate = {"command": self.gate, "rc": rcg}
            (wt.parent / f"{wt.name}.gate.log").write_text(outg, encoding="utf-8", errors="replace")
            if rcg != 0:
                rec.status, rec.note, rec.tail = "red", f"the final gate failed (exit {rcg})", outg[-TAIL:]


def push_command(entry: dict, remote: str = "origin") -> str:
    """What an approver runs to push an approved entry (shown, never executed, by the conductor)."""
    return f"git push {shlex.quote(remote)} {shlex.quote(entry['branch'])}"
