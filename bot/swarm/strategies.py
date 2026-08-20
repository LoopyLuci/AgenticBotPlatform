"""Built-in swarm strategies. Each `config` shape is validated by
bot/dashboard/server.py before a swarm is saved (every referenced
instance id must exist), so strategies here can assume well-formed input.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from bot.backends.base import BackendError
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


class FanoutSynthesizeStrategy(SwarmStrategy):
    name = "fanout_synthesize"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        members: list[int] = cfg.get("members") or []
        synthesizer = cfg.get("synthesizer")
        if not members:
            raise SwarmStrategyError("fanout_synthesize needs at least one member")

        async def _one(instance_id: int) -> dict[str, Any]:
            step = {"step": instance_label(instance_id), "instance_id": instance_id, "role": "worker"}
            try:
                text = await run_member(instance_id, prompt, swarm_run_id=swarm_run_id, user_id=user_id)
                step["status"] = "success"
                step["result"] = text
            except BackendError as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
            return step

        steps = await asyncio.gather(*[_one(m) for m in members])
        steps = list(steps)
        succeeded = [(s["step"], s["result"]) for s in steps if s["status"] == "success"]

        if not succeeded:
            return SwarmRunResult(status="failed", result=None, steps=steps, error="every member failed")

        if synthesizer:
            syn_step = {"step": instance_label(synthesizer), "instance_id": synthesizer, "role": "synthesizer"}
            try:
                final = await run_member(
                    synthesizer, _synthesis_prompt(prompt, succeeded), swarm_run_id=swarm_run_id, user_id=user_id
                )
                syn_step["status"] = "success"
                syn_step["result"] = final
                steps.append(syn_step)
                status = "success" if len(succeeded) == len(members) else "partial"
                return SwarmRunResult(status=status, result=final, steps=steps)
            except BackendError as exc:
                syn_step["status"] = "failed"
                syn_step["error"] = str(exc)
                steps.append(syn_step)
                return SwarmRunResult(status="failed", result=None, steps=steps, error=f"synthesizer failed: {exc}")

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

        async def _one(instance_id: int) -> dict[str, Any]:
            step = {"step": instance_label(instance_id), "instance_id": instance_id, "role": "worker"}
            try:
                text = await run_member(instance_id, prompt, swarm_run_id=swarm_run_id, user_id=user_id)
                step["status"] = "success"
                step["result"] = text
            except BackendError as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
            return step

        steps = list(await asyncio.gather(*[_one(m) for m in members]))
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
        leader_step = {"step": instance_label(leader), "instance_id": leader, "role": "leader"}
        try:
            final = await run_member(leader, leader_prompt, swarm_run_id=swarm_run_id, user_id=user_id)
            leader_step["status"] = "success"
            leader_step["result"] = final
            steps.append(leader_step)
            status = "success" if len(succeeded) == len(members) else "partial"
            return SwarmRunResult(status=status, result=final, steps=steps)
        except BackendError as exc:
            leader_step["status"] = "failed"
            leader_step["error"] = str(exc)
            steps.append(leader_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error=f"leader failed: {exc}")


class SequentialRelayStrategy(SwarmStrategy):
    name = "sequential_relay"

    async def run(self, swarm_row, prompt, *, swarm_run_id, user_id=0) -> SwarmRunResult:
        cfg = swarm_row["config"]
        members: list[Any] = cfg.get("members") or []
        if not members:
            raise SwarmStrategyError("sequential_relay needs at least one member")

        steps: list[dict[str, Any]] = []
        current_prompt = prompt
        last_text = None
        for i, member in enumerate(members):
            instance_id = member["instance_id"] if isinstance(member, dict) else member
            instruction = member.get("instruction") if isinstance(member, dict) else None
            step_prompt = current_prompt
            if i > 0:
                step_prompt = (
                    f"{prompt}\n\n---\nPrevious step's output:\n{last_text}\n\n"
                    f"{instruction or 'Continue/refine based on the above.'}"
                )
            step = {"step": instance_label(instance_id), "instance_id": instance_id, "role": "relay"}
            try:
                text = await run_member(instance_id, step_prompt, swarm_run_id=swarm_run_id, user_id=user_id)
                step["status"] = "success"
                step["result"] = text
                last_text = text
            except BackendError as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
                steps.append(step)
                return SwarmRunResult(status="failed", result=None, steps=steps, error=f"{step['step']} failed: {exc}")
            steps.append(step)

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
        planner_step = {"step": instance_label(planner), "instance_id": planner, "role": "planner"}
        try:
            plan_text = await run_member(planner, plan_prompt, swarm_run_id=swarm_run_id, user_id=user_id)
        except BackendError as exc:
            planner_step["status"] = "failed"
            planner_step["error"] = str(exc)
            steps.append(planner_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error=f"planner failed: {exc}")

        try:
            start = plan_text.index("[")
            end = plan_text.rindex("]") + 1
            subtasks = json.loads(plan_text[start:end])
            subtask_texts = [s["subtask"] for s in subtasks if s.get("subtask")]
            if not subtask_texts:
                raise ValueError("empty subtask list")
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            planner_step["status"] = "failed"
            planner_step["error"] = f"planner didn't return valid JSON: {exc}"
            steps.append(planner_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error="planner output wasn't valid JSON — no retry in this version")

        planner_step["status"] = "success"
        planner_step["result"] = plan_text
        steps.append(planner_step)

        async def _one(instance_id: int, subtask: str) -> dict[str, Any]:
            step = {"step": instance_label(instance_id), "instance_id": instance_id, "role": "worker", "subtask": subtask}
            try:
                text = await run_member(instance_id, subtask, swarm_run_id=swarm_run_id, user_id=user_id)
                step["status"] = "success"
                step["result"] = text
            except BackendError as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
            return step

        # round-robin assignment over the available members
        pairs = [(members[i % len(members)], subtask) for i, subtask in enumerate(subtask_texts)]
        worker_steps = list(await asyncio.gather(*[_one(m, s) for m, s in pairs]))
        steps.extend(worker_steps)
        succeeded = [(s["step"], s["result"]) for s in worker_steps if s["status"] == "success"]
        if not succeeded:
            return SwarmRunResult(status="failed", result=None, steps=steps, error="every subtask worker failed")

        agg_step = {"step": instance_label(aggregator), "instance_id": aggregator, "role": "aggregator"}
        try:
            final = await run_member(
                aggregator, _synthesis_prompt(prompt, succeeded), swarm_run_id=swarm_run_id, user_id=user_id
            )
            agg_step["status"] = "success"
            agg_step["result"] = final
            steps.append(agg_step)
            status = "success" if len(succeeded) == len(worker_steps) else "partial"
            return SwarmRunResult(status=status, result=final, steps=steps)
        except BackendError as exc:
            agg_step["status"] = "failed"
            agg_step["error"] = str(exc)
            steps.append(agg_step)
            return SwarmRunResult(status="failed", result=None, steps=steps, error=f"aggregator failed: {exc}")


class CustomStrategy(SwarmStrategy):
    """config: {"steps": [{"id","instance_id","depends_on": [...], "role"?}, ...]}
    Executed by dependency order — steps with all dependencies satisfied
    run concurrently via asyncio.gather(); a step's prompt is the original
    prompt plus every dependency's labeled output when it has any."""

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

        results: dict[str, dict[str, Any]] = {}
        remaining = {s["id"]: s for s in step_defs}
        all_steps: list[dict[str, Any]] = []

        while remaining:
            ready = [
                s for s in remaining.values()
                if all(dep in results for dep in (s.get("depends_on") or []))
            ]
            if not ready:
                raise SwarmStrategyError("custom swarm graph is stuck — unresolved dependencies (cycle?)")

            async def _run_step(step_def: dict[str, Any]) -> dict[str, Any]:
                instance_id = step_def["instance_id"]
                deps = step_def.get("depends_on") or []
                if deps:
                    context_text = "\n\n".join(
                        f"— {by_id[d].get('id')} —\n{results[d]['result']}" for d in deps if results[d]["status"] == "success"
                    )
                    step_prompt = f"{prompt}\n\n---\nInputs from prior steps:\n{context_text}"
                else:
                    step_prompt = prompt
                out = {
                    "step": step_def["id"], "instance_id": instance_id,
                    "role": step_def.get("role", "worker"),
                }
                try:
                    text = await run_member(instance_id, step_prompt, swarm_run_id=swarm_run_id, user_id=user_id)
                    out["status"] = "success"
                    out["result"] = text
                except BackendError as exc:
                    out["status"] = "failed"
                    out["error"] = str(exc)
                return out

            done = await asyncio.gather(*[_run_step(s) for s in ready])
            for out in done:
                results[out["step"]] = out
                all_steps.append(out)
                remaining.pop(out["step"], None)

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
