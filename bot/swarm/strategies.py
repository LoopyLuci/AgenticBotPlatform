"""Built-in swarm strategies. Each `config` shape is validated by
bot/dashboard/server.py before a swarm is saved (every referenced
instance id must exist), so strategies here can assume well-formed input.

The steps run as tasks on the work board (bot/swarm/board_run.py): ordering through deps, workers pulling with
`next`, results recorded on the tasks, repeated dispatches deduplicated.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bot.swarm.board_run import Step, SwarmBoard
from bot.swarm.base import SwarmRunResult, SwarmStrategy, SwarmStrategyError, instance_label, run_member

logger = logging.getLogger("bot.swarm.strategies")


def _synthesis_prompt(original_prompt: str, answers: list[tuple[str, str]]) -> str:
    labeled = "\n\n".join(f"{i+1}) {name}:\n{text}" for i, (name, text) in enumerate(answers))
    return (
        f"Here are {len(answers)} independent answers to the question below. "
        f"Produce one merged final answer — reconcile disagreements, keep what's "
        f"correct and useful from each, and don't just concatenate them.\n\n"
        f"Question: {original_prompt}\n\nAnswers:\n{labeled}"
    )


def _board(swarm_row: dict[str, Any], prompt: str, swarm_run_id: str, user_id: int) -> SwarmBoard:
    async def call(instance_id: int, text: str) -> str:
        return await run_member(instance_id, text, swarm_run_id=swarm_run_id, user_id=user_id)

    return SwarmBoard(swarm_row, prompt, swarm_run_id, call)


def _step(sid: str, instance_id: int, role: str, text, **kw: Any) -> Step:
    """A step whose prompt is fixed text, or a function of the finished steps it depends on."""
    fn = text if callable(text) else (lambda _o, _t=text: _t)
    return Step(id=sid, instance_id=instance_id, role=role, label=instance_label(instance_id), prompt=fn, **kw)


def _out(outcomes: dict[str, dict], step: Step) -> dict[str, Any]:
    return outcomes.get(step.id) or {"step": step.label, "instance_id": step.instance_id, "role": step.role,
                                     **step.extra, "status": "failed", "error": "the step did not run"}


class FanoutSynthesizeStrategy(SwarmStrategy):
    name = "fanout_synthesize"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        members: list[int] = cfg.get("members") or []
        synthesizer = cfg.get("synthesizer")
        if not members:
            raise SwarmStrategyError("fanout_synthesize needs at least one member")

        board = _board(swarm_row, prompt, swarm_run_id, user_id)
        defs = [_step(f"w{i}", m, "worker", prompt) for i, m in enumerate(members)]
        done = await board.run(defs)
        steps = [_out(done, d) for d in defs]
        succeeded = [(s["step"], s["result"]) for s in steps if s["status"] == "success"]

        if not succeeded:
            return SwarmRunResult(status="failed", result=None, steps=steps, error="every member failed")

        if synthesizer:
            sdef = _step("synth", synthesizer, "synthesizer", _synthesis_prompt(prompt, succeeded))
            syn_step = _out(await board.run([sdef]), sdef)
            steps.append(syn_step)
            if syn_step["status"] == "success":
                status = "success" if len(succeeded) == len(members) else "partial"
                return SwarmRunResult(status=status, result=syn_step["result"], steps=steps)
            return SwarmRunResult(status="failed", result=None, steps=steps, error=f"synthesizer failed: {syn_step.get('error')}")

        final = "\n\n".join(f"— {name} —\n{text}" for name, text in succeeded)
        status = "success" if len(succeeded) == len(members) else "partial"
        return SwarmRunResult(status=status, result=final, steps=steps)


class LeaderVoteStrategy(SwarmStrategy):
    name = "leader_vote"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        members: list[int] = cfg.get("members") or []
        leader = cfg.get("leader")
        if not members:
            raise SwarmStrategyError("leader_vote needs at least one member")
        if not leader:
            raise SwarmStrategyError("leader_vote needs a leader")

        board = _board(swarm_row, prompt, swarm_run_id, user_id)
        defs = [_step(f"w{i}", m, "worker", prompt) for i, m in enumerate(members)]
        done = await board.run(defs)
        steps = [_out(done, d) for d in defs]
        succeeded = [(s["step"], s["result"]) for s in steps if s["status"] == "success"]
        if not succeeded:
            return SwarmRunResult(status="failed", result=None, steps=steps, error="every member failed")

        leader_prompt = (
            f"Given these {len(succeeded)} candidate answers to the question below, "
            f"pick the single best one (or synthesize a better one if none is fully correct). "
            f"Respond with only the final answer, no commentary about the selection process.\n\n"
            f"Question: {prompt}\n\n"
            + "\n\n".join(f"{i+1}) {name}:\n{text}" for i, (name, text) in enumerate(succeeded))
        )
        ldef = _step("leader", leader, "leader", leader_prompt)
        leader_step = _out(await board.run([ldef]), ldef)
        steps.append(leader_step)
        if leader_step["status"] == "success":
            status = "success" if len(succeeded) == len(members) else "partial"
            return SwarmRunResult(status=status, result=leader_step["result"], steps=steps)
        return SwarmRunResult(status="failed", result=None, steps=steps, error=f"leader failed: {leader_step.get('error')}")


class SequentialRelayStrategy(SwarmStrategy):
    name = "sequential_relay"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        members: list[Any] = cfg.get("members") or []
        if not members:
            raise SwarmStrategyError("sequential_relay needs at least one member")

        def link(i: int, member: Any) -> Step:
            instance_id = member["instance_id"] if isinstance(member, dict) else member
            instruction = member.get("instruction") if isinstance(member, dict) else None

            def text(done: dict[str, dict]) -> str:
                if i == 0:
                    return prompt
                return (
                    f"{prompt}\n\n---\nPrevious step's output:\n{done[f'r{i-1}']['result']}\n\n"
                    f"{instruction or 'Continue/refine based on the above.'}"
                )

            return _step(f"r{i}", instance_id, "relay", text, deps=[f"r{i-1}"] if i else [])

        board = _board(swarm_row, prompt, swarm_run_id, user_id)
        defs = [link(i, m) for i, m in enumerate(members)]
        done = await board.run(defs)       # a failed link leaves everything after it blocked, and the board drops it

        steps: list[dict[str, Any]] = []
        last_text = None
        for d in defs:
            step = done.get(d.id)
            if step is None:
                break
            steps.append(step)
            if step["status"] != "success":
                return SwarmRunResult(status="failed", result=None, steps=steps, error=f"{step['step']} failed: {step.get('error')}")
            last_text = step["result"]
        return SwarmRunResult(status="success", result=last_text, steps=steps)


class DecomposeDelegateStrategy(SwarmStrategy):
    name = "decompose_delegate"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        planner = cfg.get("planner")
        members: list[int] = cfg.get("members") or []
        aggregator = cfg.get("aggregator")
        if not (planner and members and aggregator):
            raise SwarmStrategyError("decompose_delegate needs a planner, at least one member, and an aggregator")

        steps: list[dict[str, Any]] = []
        plan_prompt = (
            f"Break the following task into up to {len(members)} independent subtasks that can be "
            f"worked on in parallel. Respond with ONLY a JSON array like "
            f'[{{"subtask": "..."}}, {{"subtask": "..."}}] and nothing else.\n\nTask: {prompt}'
        )
        board = _board(swarm_row, prompt, swarm_run_id, user_id)
        pdef = _step("planner", planner, "planner", plan_prompt)
        planner_step = _out(await board.run([pdef]), pdef)
        if planner_step["status"] != "success":
            steps.append(planner_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error=f"planner failed: {planner_step.get('error')}")
        plan_text = planner_step["result"]

        try:
            start = plan_text.index("[")
            end = plan_text.rindex("]") + 1
            subtasks = json.loads(plan_text[start:end])
            # Models also answer with a bare list of strings; accept both shapes.
            subtask_texts = [s if isinstance(s, str) else s.get("subtask") for s in subtasks]
            subtask_texts = [s.strip() for s in subtask_texts if isinstance(s, str) and s.strip()]
            if not subtask_texts:
                raise ValueError("empty subtask list")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            planner_step = {k: v for k, v in planner_step.items() if k != "result"}
            planner_step["status"] = "failed"
            planner_step["error"] = f"planner didn't return valid JSON: {exc}"
            steps.append(planner_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error="planner output wasn't valid JSON — no retry in this version")

        steps.append(planner_step)

        # A planner that ignores "up to N" must not fan out without bound.
        max_subtasks = int(cfg.get("max_subtasks") or len(members) * 2)
        if len(subtask_texts) > max_subtasks:
            logger.warning("planner returned %d subtasks; running the first %d", len(subtask_texts), max_subtasks)
            subtask_texts = subtask_texts[:max_subtasks]
        # round-robin assignment over the available members
        wdefs = [
            _step(f"t{i}", members[i % len(members)], "worker", subtask, deps=["planner"], extra={"subtask": subtask})
            for i, subtask in enumerate(subtask_texts)
        ]
        done = await board.run(wdefs)
        worker_steps = [_out(done, d) for d in wdefs]
        steps.extend(worker_steps)
        succeeded = [(s["step"], s["result"]) for s in worker_steps if s["status"] == "success"]
        if not succeeded:
            return SwarmRunResult(status="failed", result=None, steps=steps, error="every subtask worker failed")

        adef = _step("aggregator", aggregator, "aggregator", _synthesis_prompt(prompt, succeeded), deps=["planner"])
        agg_step = _out(await board.run([adef]), adef)
        steps.append(agg_step)
        if agg_step["status"] == "success":
            status = "success" if len(succeeded) == len(worker_steps) else "partial"
            return SwarmRunResult(status=status, result=agg_step["result"], steps=steps)
        return SwarmRunResult(status="failed", result=None, steps=steps, error=f"aggregator failed: {agg_step.get('error')}")


class CustomStrategy(SwarmStrategy):
    """config: {"steps": [{"id","instance_id","depends_on": [...], "role"?}, ...]}
    Executed by dependency order - steps with all dependencies satisfied run concurrently (the board hands them to
    workers); a step's prompt is the original prompt plus every dependency's labeled output when it has any. A failed
    step still unblocks its dependents (they just get no input from it)."""

    name = "custom"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        step_defs: list[dict[str, Any]] = cfg.get("steps") or []
        if not step_defs:
            raise SwarmStrategyError("custom swarm needs at least one step")

        by_id = {s["id"]: s for s in step_defs}
        for s in step_defs:
            for dep in s.get("depends_on") or []:
                if dep not in by_id:
                    raise SwarmStrategyError(f"step {s['id']!r} depends on unknown step {dep!r}")
        self._check_cycles(step_defs)

        def make(step_def: dict[str, Any]) -> Step:
            deps = list(step_def.get("depends_on") or [])

            def text(done: dict[str, dict]) -> str:
                if not deps:
                    return prompt
                context_text = "\n\n".join(
                    f"— {d} —\n{done[d]['result']}" for d in deps if d in done and done[d]["status"] == "success"
                )
                return f"{prompt}\n\n---\nInputs from prior steps:\n{context_text}"

            return Step(id=step_def["id"], instance_id=step_def["instance_id"], role=step_def.get("role", "worker"),
                        label=step_def["id"], prompt=text, deps=deps, tolerate=True)

        board = _board(swarm_row, prompt, swarm_run_id, user_id)
        defs = [make(s) for s in step_defs]
        done = await board.run(defs)

        level: dict[str, int] = {}

        def depth(sid: str) -> int:
            if sid not in level:
                level[sid] = 1 + max([depth(d) for d in by_id[sid].get("depends_on") or []], default=-1)
            return level[sid]

        ordered = sorted(defs, key=lambda d: depth(d.id))          # the order the dependency waves would have run in
        all_steps = [_out(done, d) for d in ordered]
        results = {d.id: out for d, out in zip(ordered, all_steps)}

        # the result is the last-executed step with no other step depending on it
        depended_on = {d for s in step_defs for d in (s.get("depends_on") or [])}
        terminal = [s["id"] for s in step_defs if s["id"] not in depended_on]
        final_results = [results[t] for t in terminal if results[t]["status"] == "success"]
        if not final_results:
            return SwarmRunResult(status="failed", result=None, steps=all_steps, error="no terminal step succeeded")
        final = "\n\n".join(f"— {r['step']} —\n{r['result']}" for r in final_results)
        all_ok = all(s["status"] == "success" for s in all_steps)
        return SwarmRunResult(status="success" if all_ok else "partial", result=final, steps=all_steps)

    @staticmethod
    def _check_cycles(step_defs: list[dict[str, Any]]) -> None:
        state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

        def visit(step_id: str, by_id: dict[str, dict[str, Any]]) -> None:
            state[step_id] = 1
            for dep in by_id[step_id].get("depends_on") or []:
                if state.get(dep, 0) == 1:
                    raise SwarmStrategyError("cycle detected in custom swarm graph")
                if state.get(dep, 0) == 0:
                    visit(dep, by_id)
            state[step_id] = 2

        by_id = {s["id"]: s for s in step_defs}
        for s in step_defs:
            if state.get(s["id"], 0) == 0:
                visit(s["id"], by_id)


STRATEGIES: dict[str, type[SwarmStrategy]] = {
    "fanout_synthesize": FanoutSynthesizeStrategy,
    "leader_vote": LeaderVoteStrategy,
    "sequential_relay": SequentialRelayStrategy,
    "decompose_delegate": DecomposeDelegateStrategy,
    "custom": CustomStrategy,
}
