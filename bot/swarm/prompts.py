"""Goal-prompt templates for dispatch_swarm_goal (Phase 3 of the
Hermes-swarm plan).

These exist as reviewable, testable text — not string concatenation
scattered through the MCP tool/dashboard route — because the prompt IS
the actual control surface here. delegate_task cannot be invoked as an
external RPC (see bot/hermes_config.py's module docstring and the plan
file for why); the only lever BotServer has over a Hermes instance's
delegation behavior, beyond configuring delegation.provider/model ahead
of time, is what this prompt asks it to do with delegate_task once it's
running.
"""

from __future__ import annotations

from typing import Optional


def hermes_delegation_goal(
    goal: str,
    *,
    worker_provider: Optional[str] = None,
    worker_model: Optional[str] = None,
    max_children: Optional[int] = None,
) -> str:
    """Builds the prompt sent to a Hermes-backed bot instance to make it
    decompose `goal` into parallel subtasks via its own delegate_task
    tool. Worker provider/model is stated explicitly in the prompt text
    (in addition to being set as the instance's delegation.provider/model
    default via configure_delegation()) because a model can still choose
    to omit config-implied routing from its own reasoning — spelling it
    out in the instructions makes the intended routing an explicit part
    of the task, not just an ambient default it might not think to rely
    on."""
    routing_note = (
        f"Each subtask should run on {worker_provider}/{worker_model} "
        "(already configured as this session's delegation default)."
        if worker_provider and worker_model
        else "Each subtask should run on this session's currently-configured delegation provider/model."
    )
    concurrency_note = (
        f"Run up to {max_children} subtasks in parallel." if max_children else "Run subtasks in parallel where independent."
    )
    return (
        "You have access to the delegate_task tool for spawning sub-agents. "
        "Break the following goal into independent subtasks and use delegate_task "
        "(role=leaf, batch mode — pass every subtask as one `tasks` array in a single call "
        "so they run in parallel, not one call per subtask) to run them. "
        f"{routing_note} {concurrency_note} "
        "Once every subtask finishes, synthesize their results into one clear final answer "
        "covering everything the goal asked for.\n\n"
        "Then, as the very last thing in your reply, include one fenced ```json code block "
        "listing every subtask you actually ran, in this exact shape (a JSON array, one object "
        'per subtask): [{"index": 0, "goal": "<the subtask\'s own goal text>", '
        '"model": "<provider/model it ran on>", "status": "ok" or "error", '
        '"result_excerpt": "<a short, 1-2 sentence summary of that subtask\'s own result>"}]. '
        "This lets the operator see a per-subtask breakdown, not just your final synthesis — "
        "include it even if there was only one subtask.\n\n"
        f"Goal: {goal}"
    )
