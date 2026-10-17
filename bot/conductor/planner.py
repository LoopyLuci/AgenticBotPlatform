"""The planner: a goal -> a small plan of board tasks.

A goal is free text, a roadmap row or a TODO line (`goal_from("TODO.md:12")` reads one line of a file). The plan is a handful of
tasks, each with

    key      stable: conductor:<plan id>:<task id>, so planning the same goal twice joins the tasks already on the board
    deps     task ids that must be done first (the plan is acyclic)
    scope    the board locks the task takes while it runs: its worktree, its branch and the files it names
    proof    a command or check that shows the task is done (a test command, or a plain check)
    capacity "" | "local" | "cloud" (bot/capacity/follow.py): cloud-hinted tasks wait while capacity is local-only

A local model writes the plan as strict JSON (llm.py: never a cloud model). The answer is validated; small defects are repaired
(odd ids, unknown deps, duplicate ids); anything else goes back to the model once or twice with the exact errors; if it still
does not hold up, or no local model answers, a deterministic plan is used (`planner: "fallback"` in the plan says so).
The plan is stored by the goal's id, so the same goal returns the same plan until it is asked to replan.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.conductor import llm, store

SCHEMA_HELP = """Reply with ONLY one JSON object:
{"tasks": [{"id": "short-slug", "title": "imperative, under 100 chars", "detail": "what to change and why",
  "depends_on": ["ids of tasks that must finish first"], "files": ["relative/paths the task touches"],
  "proof": {"command": "ONLY `python -m pytest <test files or node ids> -q` or `ruff check <paths>`; no other command, no shell syntax",
            "description": "what the proof shows"},
  "capacity": "local"}]}
Rules: 1 to {max_tasks} tasks, each small enough for one focused change; ids are lowercase letters, digits and dashes; no cycles;
every task has a proof command; paths are relative to the repository; "capacity" is "local" (routine) or "cloud" (needs a strong
model) or "". Never remove or rename an existing feature (a command, flag, route, tool, page or setting)."""

_ID = re.compile(r"[^a-z0-9]+")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
MAX_REPAIRS = 2


class PlanError(ValueError):
    pass


@dataclass
class PlannedTask:
    id: str
    title: str
    key: str = ""
    detail: str = ""
    deps: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    proof: dict = field(default_factory=dict)
    capacity: str = ""
    priority: int = 0
    branch: str = ""
    worktree: str = ""


@dataclass
class Plan:
    id: str
    goal: str
    source: str = ""
    planner: str = ""                 # "model:<ref>" | "fallback"
    repaired: int = 0                 # model answers sent back for repair
    notes: list[str] = field(default_factory=list)
    tasks: list[PlannedTask] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(**{**d, "tasks": [PlannedTask(**t) for t in d.get("tasks") or []]})


# ------------------------------------------------------------------ goals
def goal_from(text: str, root: Optional[Path] = None) -> tuple[str, str]:
    """(goal text, source). `FILE:LINE` reads that line of the file (a roadmap row, a TODO line) with its list or table markup
    stripped; anything else is the goal as typed."""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(.+?):(\d+)", raw)
    if m:
        path = Path(m.group(1))
        if root and not path.is_absolute():
            path = root / path
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            n = int(m.group(2))
            if 1 <= n <= len(lines):
                line = lines[n - 1]
                line = re.sub(r"^\s*(?:[-*+]\s*(?:\[[ xX]\]\s*)?|\d+[.)]\s*)", "", line).strip()
                cells = [c.strip() for c in line.strip("|").split("|")] if line.startswith("|") else [line]
                return " - ".join(c for c in cells if c and not set(c) <= set("-: "))[:2000], f"{m.group(1)}:{n}"
    return raw[:2000], ""


def plan_exists(goal_text: str, root: Optional[Path] = None) -> bool:
    return bool(store.get_plan(plan_id(goal_from(goal_text, root)[0])))


def plan_id(goal: str) -> str:
    return hashlib.sha256(" ".join(goal.lower().split()).encode("utf-8")).hexdigest()[:12]


def slug(text: str, limit: int = 40) -> str:
    return _ID.sub("-", str(text or "").lower()).strip("-")[:limit].strip("-")


# ------------------------------------------------------------ validation
def extract_json(text: str) -> Any:
    """The JSON object in a model's answer: bare, fenced, or surrounded by talk."""
    body = _FENCE.sub("", str(text or "").strip())
    try:
        return json.loads(body)
    except ValueError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 < end:
        try:
            return json.loads(body[start:end + 1])
        except ValueError:
            pass
    raise PlanError("the answer is not a JSON object")


def _clean_path(p: Any) -> Optional[str]:
    p = str(p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        return None
    return p[:200]


def validate(data: Any, max_tasks: int = 8) -> tuple[list[dict], list[str]]:
    """(the tasks, repaired where it was safe to; the errors that remain). Errors empty = a usable plan."""
    errors: list[str] = []
    raw = data.get("tasks") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(raw, list) or not raw:
        return [], ['"tasks" must be a non-empty list']
    if len(raw) > max_tasks:
        errors.append(f"too many tasks ({len(raw)}); at most {max_tasks}, merge small ones")
        raw = raw[:max_tasks]
    tasks: list[dict] = []
    seen: set[str] = set()
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            errors.append(f"task {i} is not an object")
            continue
        title = str(t.get("title") or "").strip()[:200]
        if not title:
            errors.append(f"task {i} has no title")
            continue
        tid = slug(t.get("id") or title) or f"t{i}"
        base, n = tid, 2
        while tid in seen:                                    # duplicate ids: repaired
            tid, n = f"{base}-{n}", n + 1
        seen.add(tid)
        proof = t.get("proof")
        if isinstance(proof, str):
            proof = {"command": proof, "description": ""}
        if not isinstance(proof, dict) or not str(proof.get("command") or proof.get("check") or "").strip():
            errors.append(f"task {tid} has no proof (a command or check that shows it is done)")
            proof = {}
        else:
            proof = {"command": str(proof.get("command") or proof.get("check")).strip()[:500],
                     "description": str(proof.get("description") or "")[:300]}
            from bot.conductor import proof as proof_check

            why = proof_check.check(proof["command"])
            if why:
                errors.append(f"task {tid}: proof not allowed ({why}); use `python -m pytest <test paths>` or `ruff check <paths>`")
                proof = {}
        cap = str(t.get("capacity") or "").strip().lower()
        if cap not in ("", "local", "cloud"):
            cap = ""
        files = [f for f in (_clean_path(p) for p in (t.get("files") or []) if isinstance(p, (str, Path))) if f][:12]
        deps = [slug(d) for d in (t.get("depends_on") or t.get("deps") or []) if isinstance(d, str)]
        tasks.append({"id": tid, "title": title, "detail": str(t.get("detail") or "")[:3000], "deps": deps, "files": files,
                      "proof": proof, "capacity": cap})
    ids = {t["id"] for t in tasks}
    for t in tasks:                                            # unknown deps and self-deps are dropped (repaired)
        t["deps"] = [d for d in dict.fromkeys(t["deps"]) if d in ids and d != t["id"]]
    cycle = _cycle(tasks)
    if cycle:
        errors.append("the dependencies form a cycle: " + " -> ".join(cycle))
    return tasks, errors


def _cycle(tasks: list[dict]) -> list[str]:
    deps = {t["id"]: list(t["deps"]) for t in tasks}
    state: dict[str, int] = {}

    def visit(n: str, trail: list[str]) -> list[str]:
        if state.get(n) == 1:
            return trail[trail.index(n):] + [n]
        if state.get(n) == 2:
            return []
        state[n] = 1
        for d in deps.get(n, []):
            hit = visit(d, trail + [n])
            if hit:
                return hit
        state[n] = 2
        return []

    for n in deps:
        hit = visit(n, [])
        if hit:
            return hit
    return []


def topo_order(tasks: list[PlannedTask]) -> list[PlannedTask]:
    done: set[str] = set()
    out: list[PlannedTask] = []
    todo = list(tasks)
    while todo:
        ready = [t for t in todo if all(d in done for d in t.deps)]
        if not ready:
            raise PlanError("the plan has a dependency cycle")
        for t in ready:
            todo.remove(t)
            done.add(t.id)
            out.append(t)
    return out


# --------------------------------------------------------------- building
def build(goal: str, raw_tasks: list[dict], *, source: str = "", planner: str, repo: str = "abp", worktree_root: str = "",
          repaired: int = 0, notes: Optional[list[str]] = None) -> Plan:
    pid = plan_id(goal)
    plan = Plan(id=pid, goal=goal, source=source, planner=planner, repaired=repaired, notes=list(notes or []))
    for i, t in enumerate(raw_tasks):
        branch = f"feat/cond-{pid[:8]}-{t['id']}"
        wt = f"{worktree_root.rstrip('/')}/{pid[:8]}-{t['id']}" if worktree_root else ""
        resources = ([f"worktree:{wt}"] if wt else []) + [f"branch:{repo}:{branch}"] + [f"path:{repo}:{f}" for f in t["files"]]
        plan.tasks.append(PlannedTask(
            id=t["id"], title=t["title"], key=f"conductor:{pid}:{t['id']}", detail=t["detail"], deps=t["deps"], files=t["files"],
            scope={"resources": resources}, proof=t["proof"], capacity=t["capacity"], priority=len(raw_tasks) - i,
            branch=branch, worktree=wt))
    topo_order(plan.tasks)                                      # raises PlanError on a cycle
    return plan


def fallback_tasks(goal: str, max_tasks: int = 8, proof_command: str = "python -m pytest tests -q") -> list[dict]:
    """The deterministic plan: one task per line or numbered/bulleted item of the goal (a single task for one sentence), each
    after the one before it, each proved by the repository's test command."""
    parts = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", ln).strip() for ln in goal.splitlines()]
    parts = [p for p in parts if p]
    if len(parts) == 1 and ";" in parts[0]:
        parts = [p.strip() for p in parts[0].split(";") if p.strip()]
    parts = parts[:max_tasks] or [goal.strip() or "goal"]
    tasks, prev = [], ""
    for i, p in enumerate(parts, 1):
        tid = slug(p, 30) or f"step-{i}"
        if any(t["id"] == tid for t in tasks):
            tid = f"{tid}-{i}"
        tasks.append({"id": tid, "title": p[:100], "detail": p if len(parts) > 1 else goal, "deps": [prev] if prev else [],
                      "files": [], "proof": {"command": proof_command, "description": "the test suite passes"}, "capacity": ""})
        prev = tid
    return tasks


CONTEXT_CAP = 9000       # characters of context (a repo map and a code slice as well as the user's text) the planner is shown


def _prompt(goal: str, max_tasks: int, context: str, cap: int = CONTEXT_CAP) -> str:
    return (f"Plan this goal for a software repository as a few small tasks.\n\nGOAL: {goal}\n" +
            (f"\nCONTEXT:\n{context[:cap]}\n" if context else "") + "\n" + SCHEMA_HELP.replace("{max_tasks}", str(max_tasks)) +
            ("\nList in \"files\" only paths that exist in the repository (see the map); a new file goes in an existing folder."
             if context else ""))


def check_files(tasks: list[dict], known: set[str]) -> list[str]:
    """Errors for task files the model invented (not in the tree, not in an existing folder)."""
    from bot.conductor import navctx

    return [f"task {t['id']}: these files do not exist in the repository: {', '.join(bad[:5])}; name real files (a new file goes in an "
            "existing folder)" for t in tasks for bad in [navctx.unknown_paths(t["files"], known)] if bad]


def drop_unknown_files(tasks: list[dict], known: set[str]) -> int:
    from bot.conductor import navctx

    n = 0
    for t in tasks:
        bad = set(navctx.unknown_paths(t["files"], known))
        n += len(bad)
        t["files"] = [f for f in t["files"] if f not in bad]
    return n


async def make_plan(goal_text: str, *, repo: str = "abp", worktree_root: str = "", context: str = "", model: str = "",
                    max_tasks: int = 8, root: Optional[Path] = None, replan: bool = False, save: bool = True,
                    context_cap: int = CONTEXT_CAP) -> Plan:
    """Plan a goal. Returns the stored plan for the same goal unless `replan`."""
    goal, source = goal_from(goal_text, root)
    if not goal:
        raise PlanError("give a goal")
    pid = plan_id(goal)
    if not replan:
        have = store.get_plan(pid)
        if have:
            return Plan.from_dict(have)
    notes: list[str] = []
    tasks: list[dict] = []
    planner, repaired = "fallback", 0
    prompt = _prompt(goal, max_tasks, context, context_cap)
    known: set[str] = set()
    if root is not None:
        from bot.conductor import navctx

        known = navctx.tracked_files(Path(root))              # empty (no check) when git cannot list the tree
    try:
        for attempt in range(MAX_REPAIRS + 1):
            text, ref = await llm.complete(prompt, system="You are a careful software planner. Answer with JSON only.",
                                           max_tokens=2500, model=model)
            try:
                tasks, errors = validate(extract_json(text), max_tasks)
            except PlanError as exc:
                tasks, errors = [], [str(exc)]
            if not errors and known:
                errors = check_files(tasks, known)
                if errors and attempt == MAX_REPAIRS:          # out of repairs: keep the plan, drop the invented paths
                    notes.append(f"dropped {drop_unknown_files(tasks, known)} invented file path(s)")
                    errors = []
            if not errors:
                planner = f"model:{ref}"
                break
            repaired += 1
            notes.append(f"model answer {attempt + 1} rejected: " + "; ".join(errors)[:300])
            prompt = (_prompt(goal, max_tasks, context, context_cap) + "\n\nYour previous answer was not usable:\n- " + "\n- ".join(errors) +
                      "\nFix these and answer with the corrected JSON only.")
            tasks = []
    except llm.NoLocalModel as exc:
        notes.append(f"no local model: {exc}")
    except Exception as exc:  # noqa: BLE001 - a model fault means the fallback plan, not no plan
        notes.append(f"model call failed: {type(exc).__name__}: {exc}"[:300])
    if not tasks:
        tasks = fallback_tasks(goal, max_tasks)
        notes.append("used the deterministic fallback plan")
    plan = build(goal, tasks, source=source, planner=planner, repo=repo, worktree_root=worktree_root, repaired=repaired, notes=notes)
    if save:
        store.save_plan(plan.to_dict())
    return plan
