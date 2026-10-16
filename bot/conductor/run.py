"""Orchestration: plan -> board tasks -> workers -> review -> integration -> push queue.

    conductor = Conductor(repo, worker)
    plan = await conductor.plan("add a --json flag to the status command")
    report = await conductor.run(plan, integrate=True)

The plan's tasks run through the existing swarm board runner (bot/swarm/board_run.py): one board task per planned task, with the
plan's deps, scope (worktree, branch and file locks), capacity hint and stable key, pulled by workers with `next`, heartbeated and
requeued by the board as for any swarm. What is new is what a task does inside a step: the **worker** makes a branch, the
**reviewer** checks the diff against the standing rules before anything depends on it, a rejection sends the reasons back to the
worker, and the second rejection escalates the task (board state `review` with an `ESCALATED:` note, then failed with the same
note, plus a row in the conductor's escalation list, which LF5 reads). Tasks that depend on an escalated one never start.

A worker is any async callable `worker(spec, feedback) -> {"branch", "worktree"?, "start"?, "created"?, "summary"?}` that leaves
its work committed on `spec["branch"]`. `AgentLoopWorker` is the real one (ABP's native agent on a local model, in a worktree);
tests use scripted ones. Models are local only (llm.py); the capacity state decides which tasks wait (capacity:cloud ones).
Nothing here pushes: green work waits in the push queue (store.py) for `approve()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bot import conductor as conductor_pkg
from bot.conductor import confine, integrator, llm, planner, proof, reviewer, store
from bot.coordination.model import Blocked

logger = logging.getLogger("bot.conductor.run")

Worker = Callable[[dict, str], Awaitable[dict]]


class Escalated(Blocked):
    """The task needs a decision (Claude or the owner): rejected too often, stuck, or a rule question. The board task goes to
    `blocked`; `context` is the minimum to decide on (kind, question, failing check, diff excerpt)."""


class WorkerStuck(RuntimeError):
    """A worker may raise this to say it cannot make progress (the conductor escalates it after the same rejections limit)."""


KINDS = ("rejections", "stuck", "rule_decision")
RULE_QUESTIONS = {"no_feature_removed": "The change removes an existing feature. May it be removed (name which), or must the worker keep it?",
                  "no_deletions": "The change deletes a file the task did not create. May it be deleted, or must the worker keep it?"}


@dataclass
class RunReport:
    plan_id: str
    tasks: dict[str, dict] = field(default_factory=dict)       # task id -> {status, branch, error, rejections}
    accepted: list[str] = field(default_factory=list)          # branches that passed review, in plan order
    escalated: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)           # waiting on an escalated or failed dependency
    integration: Optional[dict] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


async def run_proof(spec: dict, worktree: str, timeout_s: Optional[int] = None) -> Optional[reviewer.Finding]:
    """Check a task's proof and run it in the worker's worktree through the sandbox (conductor/proof.py). None when it passes; a
    finding when it is not an allowed proof (never executed) or fails."""
    cmd = str((spec.get("proof") or {}).get("command") or "").strip()
    if not cmd:
        return None
    try:
        ok, output = await proof.run(cmd, Path(worktree), timeout_s)
    except proof.ProofRefused as exc:
        return reviewer.Finding("proof", f"proof not allowed: {exc}", kind="proof", text=cmd)
    except Exception as exc:  # noqa: BLE001 - a proof that cannot run does not pass
        return reviewer.Finding("proof", f"the proof could not run: {type(exc).__name__}: {exc}"[:300], kind="proof", text=cmd)
    return None if ok else reviewer.Finding("proof", f"the proof failed: {cmd}", kind="proof", text=output[-600:])


class Conductor:
    def __init__(self, repo: Any, worker: Worker, *, board: Any = None, base: Optional[str] = None, repo_name: str = "abp",
                 model_complete: Optional[reviewer.ModelCall] = None, suite: Optional[list[str]] = None,
                 rerun: Optional[list[str]] = None, workdir: Optional[Path] = None, run_proofs: bool = True, workers: int = 0):
        self.repo = Path(repo)
        self.worker = worker
        self.board = board
        self.cfg = conductor_pkg.settings()
        self.base = base or self.cfg["base_branch"]
        self.repo_name = repo_name
        self.model_complete = model_complete
        self.suite = suite or list(self.cfg["suite_command"])
        self.rerun = rerun
        self.workdir = workdir
        self.run_proofs = run_proofs
        self.workers = int(workers)                            # parallel workers pulling with `next` (0 = the coordination setting)
        self._plan: Optional[planner.Plan] = None
        self._done: dict[str, dict] = {}                       # task id -> accepted result
        self._model_used = ""

    # ----------------------------------------------------------------- plan
    async def plan(self, goal: str, *, context: str = "", replan: bool = False) -> planner.Plan:
        from bot.agent_runtime.state import state_dir

        root = (self.workdir or state_dir("conductor", "worktrees"))
        return await planner.make_plan(goal, repo=self.repo_name, worktree_root=str(root).replace("\\", "/"), context=context,
                                       model=self.cfg["model"], max_tasks=int(self.cfg["max_tasks"]), root=self.repo, replan=replan)

    # ------------------------------------------------------------------ run
    async def run(self, plan: planner.Plan, *, integrate: bool = False) -> RunReport:
        from bot.swarm.board_run import Step, SwarmBoard

        self._plan, self._done = plan, {}
        steps = [Step(id=t.id, instance_id=0, role="conductor", label=t.title[:90], prompt=(lambda _deps, tid=t.id: tid), deps=list(t.deps),
                      capacity=t.capacity, scope=t.scope, priority=t.priority, detail=_detail(t, plan),
                      key=t.key) for t in planner.topo_order(plan.tasks)]
        sb = SwarmBoard({"id": f"conductor-{plan.id}"}, plan.goal, f"{plan.id}", self._call)
        if self.board is not None:
            sb.board = self.board
        if self.workers > 0:
            sb.max_workers = self.workers
        results = await sb.run(steps)
        report = RunReport(plan_id=plan.id)
        for t in planner.topo_order(plan.tasks):
            out = results.get(t.id)
            if out is None:
                report.not_run.append(t.id)
                report.tasks[t.id] = {"status": "not_run", "why": "a task it depends on did not finish"}
                continue
            row: dict[str, Any] = {"status": out.get("status"), "rejections": store.rejections(plan.id, t.key)}
            if out.get("status") == "success":
                data = _loads(out.get("result"))
                row.update(branch=data.get("branch", ""), review=data.get("verdict", ""))
                report.accepted.append(data.get("branch", ""))
            else:
                row["error"] = out.get("error", "")
                if str(row["error"]).startswith("ESCALATED"):
                    report.escalated.append(t.id)
            report.tasks[t.id] = row
        report.accepted = [b for b in report.accepted if b]
        if integrate and report.accepted and not report.escalated:
            report.integration = await asyncio.to_thread(self.integrate, report.accepted, plan_id=plan.id,
                                                         tasks=[t.id for t in plan.tasks])
        return report

    def integrate(self, branches: list[str], *, plan_id: str = "", tasks: Optional[list[str]] = None) -> dict:
        integ = integrator.Integrator(self.repo, base=self.base, suite=self.suite, rerun=self.rerun, workdir=self.workdir,
                                      suite_timeout_s=float(self.cfg["suite_timeout_s"]), regen_sdk=bool(self.cfg["regen_sdk"]),
                                      shards=int(self.cfg["suite_shards"]), workers=self.cfg["suite_workers"],
                                      max_workers=int(self.cfg["max_workers"]), lock_wait_s=float(self.cfg["suite_lock_wait_s"]),
                                      gate=list(self.cfg["gate_command"]) or None, gate_timeout_s=float(self.cfg["gate_timeout_s"]))
        return integ.integrate(branches, plan_id=plan_id, tasks=tasks or []).to_dict()

    # ------------------------------------------------------- one task's work
    async def _call(self, _instance: int, task_id: str) -> str:
        plan = self._plan
        assert plan is not None
        task = next(t for t in plan.tasks if t.id == task_id)
        max_rej = int(self.cfg["max_rejections"])
        spec = {"plan_id": plan.id, "goal": plan.goal, "id": task.id, "key": task.key, "title": task.title, "detail": task.detail,
                "files": task.files, "proof": task.proof, "branch": task.branch, "worktree": task.worktree, "base": self.base,
                "after": [self._done[d]["branch"] for d in task.deps if d in self._done]}
        feedback = ""
        stuck = attempt = 0
        while True:
            if store.rejections(plan.id, task.key) >= max_rej:
                await self._escalate(plan, task, "rejected twice and no worker attempt left")
            attempt += 1
            try:
                result = await self.worker(dict(spec), feedback)
            except WorkerStuck as exc:
                result = {"branch": task.branch, "empty": True, "summary": str(exc)}
            branch = str(result.get("branch") or spec["branch"])
            review_base = str(result.get("start") or (spec["after"][-1] if spec["after"] else self.base))
            if result.get("tool_calls") or result.get("tokens"):          # the confined runner's audit trail: every tool call
                store.add_tool_calls(plan.id, task.key, attempt, result.get("tool_calls") or [],
                                     f"tokens={result.get('tokens', 0)} iterations={result.get('iterations', 0)} "
                                     f"stopped={result.get('stopped', '')} shell={result.get('shell', '')}")
            if result.get("empty"):                                       # nothing to review: the agent produced no change
                stuck += 1
                feedback = ("Your previous attempt changed nothing" + (" (it ran out of steps or time)" if result.get("stopped") else "") +
                            ". Make the smallest change that does the task, and write it with the edit tools.")
                if stuck >= max_rej:
                    await self._escalate(plan, task, f"stuck: {stuck} attempts produced no change", kind="stuck",
                                         question="The worker keeps ending without a change. Is the task clear enough, or should it be "
                                                  "split, done by hand, or given to a bigger model?",
                                         base=review_base, branch=branch, summary=str(result.get("summary", "")))
                continue
            rep = await asyncio.to_thread(
                reviewer.review, self.repo, review_base, branch, created_files=result.get("created") or [],
                allow_delete=result.get("allow_delete") or [], public=bool(self.cfg["public_repo"]),
                private_dir=os.environ.get("ABP_PRIVATE_DIR") or None, run_ruff=bool(self.cfg["check_ruff"]),
                check_reload=bool(self.cfg["check_hotreload"]))
            if self.run_proofs:
                cmd = str((spec.get("proof") or {}).get("command") or "")
                why = proof.check(cmd)
                bad = reviewer.Finding("proof", f"proof not allowed: {why}", kind="proof", text=cmd) if why else None
                if bad is None and rep.accepted and result.get("worktree"):
                    bad = await run_proof(spec, str(result["worktree"]))
                if bad:
                    rep.findings.append(bad)
                    rep.checks["proof"] = "fail"
                    rep.verdict = reviewer.REJECT
                else:
                    rep.checks["proof"] = "pass"
            if rep.accepted and self.cfg["model_review"]:
                await reviewer.model_review(rep, self.repo, goal=plan.goal, complete=self.model_complete, model=self.cfg["review_model"],
                                            avoid=self.cfg["model"], diff_cap=int(self.cfg["review_diff_cap"]))
            store.add_review(plan.id, task.key, branch, rep.to_dict())
            if rep.accepted:
                self._done[task.id] = {"branch": branch, "head": rep.head}
                return json.dumps({"branch": branch, "verdict": rep.verdict, "head": rep.head, "summary": result.get("summary", ""),
                                   "checks": rep.checks, "advisory": [str(f) for f in rep.advisory][:10]})
            feedback = rep.feedback()
            if store.rejections(plan.id, task.key) >= max_rej:
                rule = next((f.rule for f in rep.findings if f.rule in RULE_QUESTIONS), "")
                await self._escalate(plan, task, f"{max_rej} review rejections; last: " + feedback.splitlines()[0][:300], rep,
                                     kind="rule_decision" if rule else "rejections", question=RULE_QUESTIONS.get(rule, ""),
                                     base=review_base, branch=branch, summary=str(result.get("summary", "")))

    async def _escalate(self, plan: planner.Plan, task: planner.PlannedTask, reason: str, rep: Optional[reviewer.Review] = None, *,
                        kind: str = "rejections", question: str = "", base: str = "", branch: str = "", summary: str = ""):
        """Stop the task for a decision: a row in the conductor's list (LF5 reads it) and the board task goes to `blocked` with the
        minimum context. Raises Escalated; the swarm runner ends the board task as blocked (not failed, not requeued)."""
        note = f"ESCALATED: {reason}"[:500]
        ctx = await asyncio.to_thread(self._blocked_context, task, reason, rep, kind, question, base, branch or task.branch, summary)
        store.escalate(plan.id, task.key, reason, {"title": task.title, "branch": task.branch,
                                                   "report": rep.to_dict() if rep else None, "blocked": ctx})
        raise Escalated(note, ctx)

    def _blocked_context(self, task: planner.PlannedTask, reason: str, rep: Optional[reviewer.Review], kind: str, question: str,
                         base: str, branch: str, summary: str) -> dict:
        diff = ""
        if base and branch:
            try:
                diff = integrator._git(self.repo, "diff", "--no-color", "-U2", f"{base}..{branch}", check=False).stdout
            except Exception:  # noqa: BLE001 - the excerpt is a convenience
                diff = ""
        finding = str(rep.findings[0])[:300] if rep is not None and rep.findings else ""
        failed = [rule for rule, state in (rep.checks if rep is not None else {}).items() if state == "fail"]
        return {"kind": kind, "task": task.title, "branch": branch, "reason": reason[:300],
                "question": question or "The review rejected this task twice. Is the reviewer right? Tell the worker what to change, or "
                                        "accept the change by hand.",
                "failing_check": (", ".join(failed) + (": " if failed and finding else "") + finding)[:400] or summary[:300],
                "diff_excerpt": diff[:1500]}

    # ---------------------------------------------------------------- views
    def status(self) -> dict:
        return status()

    def approve(self, queue_id: str, by: str, note: str = "") -> dict:
        return approve(queue_id, by, note)

    def unblock(self, plan_id: str, task_key: str, by: str, note: str = "") -> dict:
        """A decision on an escalated task: resolve its escalation (its rejection count starts afresh) and put the board task back to
        `open` so the next `run` of the plan picks it up. Boards without a blocked state (AgentSentinel's) are left as they are: there
        the task is `failed` with a BLOCKED: note and the next run re-creates it."""
        if not str(by or "").strip():
            raise ValueError("say who decides (by)")
        esc = store.open_escalation(plan_id, task_key)
        if esc is None:
            raise KeyError(f"no open escalation for {task_key}")
        store.resolve_escalation(esc["id"])
        out: dict[str, Any] = {"escalation": esc["id"], "board": "unchanged"}
        board = self.board
        if board is None:
            from bot import coordination

            board = coordination.get_board()
        for t in board.list_tasks(f"abp/swarm-conductor-{plan_id}", state="blocked", limit=500):
            if t.get("key") == task_key:
                board.unblock(t["id"], by=by, note=note)
                out["board"] = "open"
        return out


def _detail(task: planner.PlannedTask, plan: planner.Plan) -> str:
    return json.dumps({"goal": plan.goal[:500], "detail": task.detail[:1500], "proof": task.proof, "branch": task.branch,
                       "files": task.files})[:15000]


def _loads(text: Any) -> dict:
    try:
        data = json.loads(text) if isinstance(text, str) else text
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------ module views
def status() -> dict:
    """The conductor at a glance: the capacity policy, plans, escalations, recent integrations and the push queue."""
    try:
        from bot.capacity import follow

        policy = follow.policy()
    except Exception:  # noqa: BLE001
        policy = {}
    queue = store.list_queue()
    return {"capacity": {"state": policy.get("state"), "cloud_allowed": policy.get("cloud_allowed"),
                         "local_models": policy.get("local_models", [])},
            "models": "local only (ModelMistress first, then Ollama)",
            "plans": store.list_plans(10), "escalations": store.list_escalations(True, 20),
            "integrations": [{k: i.get(k) for k in ("id", "branch", "status", "plan_id", "merged", "flaky", "failed", "note")}
                             for i in store.list_integrations(10)],
            "queue": {"awaiting_approval": [q for q in queue if q["status"] == "awaiting_approval"],
                      "decided": [q for q in queue if q["status"] != "awaiting_approval"][:10]}}


def queue(status_filter: str = "") -> list[dict]:
    return store.list_queue(status_filter)


def approve(queue_id: str, by: str, note: str = "", reject: bool = False) -> dict:
    """Record the owner's (or Claude's) decision on a queued integration branch. Approving does not push: the entry says what
    to push, and the pusher (off by default; `push`) or the approver does it."""
    entry = store.decide(queue_id, "rejected" if reject else "approved", by, note)
    entry["push_command"] = integrator.push_command(entry)
    return entry


# ------------------------------------------------------------ the real worker
class AgentLoopWorker:
    """ABP's native agent on a local model, in its own git worktree and branch. Whatever it changes is committed on the branch.
    `drive` is the call that runs the agent (tests replace it); the default uses abp_agenteval.factory.drive_agent, the headless
    loop LF2 benchmarks, with small-model mode."""

    _agent_lock = threading.Lock()      # the agent's config overlay and ephemeral environment are process-global: one agent turn at a time

    def __init__(self, repo: Any, *, model: str = "", timeout_s: float = 900, max_iterations: int = 60,
                 drive: Optional[Callable[..., Awaitable[Any]]] = None, shell: str = "none", sandbox_paths: Optional[list[str]] = None):
        if shell not in ("none", "sandbox"):
            raise ValueError("shell must be 'none' (file + test tools only) or 'sandbox' (run_shell inside an AppContainer, no network)")
        self.repo = Path(repo)
        self.model = model
        self.timeout_s = timeout_s
        self.max_iterations = max_iterations
        self.shell = shell
        self.sandbox_paths = list(sandbox_paths) if sandbox_paths else None
        self.drive = drive or self._drive
        self._verified = False
        if shell == "sandbox" and not confine.supported():          # refused up front, never degraded to an unconfined shell
            raise confine.SandboxUnavailable("--shell sandbox needs a Windows AppContainer; use --shell none here")

    def prepare(self, spec: dict) -> tuple[Path, str]:
        """The task's worktree on its branch (made from the base, or the branch it builds on); its starting commit."""
        wt = Path(spec["worktree"])
        if not wt.is_dir():
            wt.parent.mkdir(parents=True, exist_ok=True)
            start = (spec.get("after") or [spec.get("base") or "main"])[-1]
            exists = integrator._git(self.repo, "rev-parse", "--verify", "-q", f"refs/heads/{spec['branch']}", check=False).returncode == 0
            if exists:
                integrator._git(self.repo, "worktree", "add", "-q", str(wt), spec["branch"])
            else:
                integrator._git(self.repo, "worktree", "add", "-q", "-b", spec["branch"], str(wt), start)
            for other in (spec.get("after") or [])[:-1]:
                integrator._git(wt, "merge", "--no-ff", "--no-edit", other)
        start_ref = (spec.get("after") or [spec.get("base") or "main"])[-1]
        base = integrator._git(wt, "merge-base", start_ref, "HEAD", check=False).stdout.strip()   # where the branch began, whatever it holds now
        return wt, base or integrator._git(wt, "rev-parse", "HEAD").stdout.strip()

    async def __call__(self, spec: dict, feedback: str) -> dict:
        wt, start = await asyncio.to_thread(self.prepare, spec)
        prompt = (f"Task: {spec['title']}\n\n{spec.get('detail') or ''}\n\nOverall goal: {spec['goal']}\n"
                  f"Files likely involved: {', '.join(spec.get('files') or []) or '(find them)'}\n"
                  f"It is done when this passes: {(spec.get('proof') or {}).get('command', '(the tests)')}\n"
                  "Rules: never remove or rename an existing feature (command, flag, route, tool, page, setting); do not delete files; "
                  "add or update a test for what you change; keep the change small." +
                  (f"\n\nYour previous attempt was REJECTED by the reviewer. Fix exactly these:\n{feedback}" if feedback else ""))
        if self.shell == "sandbox" and not self._verified:
            await asyncio.to_thread(self._verify_sandbox, wt)
        res = await self.drive(prompt, wt)
        if not isinstance(res, str) and getattr(res, "ok", True) is False and not getattr(res, "tool_calls", None):
            raise llm.WorkerModelError(f"the model did not answer: {str(getattr(res, 'error', '') or 'no reply')[:300]}")
        await asyncio.to_thread(self._commit, wt, spec)
        made = integrator._git(wt, "rev-list", "--count", f"{start}..HEAD", check=False).stdout.strip()
        out = {"branch": spec["branch"], "worktree": str(wt), "start": start, "shell": self.shell, "empty": made in ("", "0")}
        if isinstance(res, str):
            return {**out, "summary": res[:500]}
        reply = str(getattr(res, "reply", "") or getattr(res, "error", "") or "")
        return {**out, "summary": reply[:500], "tool_calls": list(getattr(res, "tool_calls", None) or []),
                "tokens": int(getattr(res, "tokens", 0) or 0), "iterations": int(getattr(res, "iterations", 0) or 0),
                "stopped": str(getattr(res, "stopped", "") or "")}

    def _verify_sandbox(self, wt: Path) -> None:
        with self._agent_lock:
            confine.verify(wt, extra_paths=self.sandbox_paths)
        self._verified = True

    @staticmethod
    def _commit(wt: Path, spec: dict) -> None:
        integrator._git(wt, "add", "-A")
        if integrator._git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
            integrator._git(wt, "commit", "-q", "-m", f"{spec['title']}\n\nconductor task {spec['key']}")

    async def _drive(self, prompt: str, wt: Path) -> str:
        from abp_agenteval import factory

        refs = llm.candidates(self.model)
        if not refs:
            raise llm.NoLocalModel("no local model is available (start ModelMistress or Ollama)")
        transport, name = llm.transport_for_ref(refs[0])
        paths = (self.sandbox_paths or confine.sandbox_paths()) if self.shell == "sandbox" else None

        def turn():
            with self._agent_lock:
                return factory.drive_agent(prompt, wt, transport, name, timeout_s=self.timeout_s, max_iterations=self.max_iterations,
                                           python=sys.executable, shell=self.shell, sandbox_paths=paths)
        # shell "none": file tools + run_tests only, no shell tool at all. "sandbox": run_shell inside an AppContainer, no network.
        return await asyncio.to_thread(turn)


def push(queue_id: str, by: str, **kw) -> dict:
    """Push an approved queue entry with the pusher (conductor.pusher.enabled must be true). Raises pusher.PushRefused."""
    from bot.conductor import pusher

    return pusher.push(queue_id, by, **kw)
