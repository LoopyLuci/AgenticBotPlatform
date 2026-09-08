"""Anthropic Message Batches API — async, bulk, non-urgent completions at
a real ~50% cost discount versus a live call (Phase F of the Claude API/
Claude Code parity plan).

Real, confirmed API constraint that reshapes this module's scope versus
a naive reading of "batch dispatch": a batch request is one complete,
single-shot Messages API call — there is no synchronous channel to feed
a `tool_result` back mid-batch, so a batched completion cannot run
BotServer's own interactive tool-calling loop (`tool_loop.run_one_tool()`)
the way a live `spawn_subagent` child does. This module is therefore a
genuinely different, narrower capability: plain (no-tools) completions,
submitted in bulk and fetched later — NOT a drop-in `spawn_subagent(batch=True)`
variant, which would have silently promised tool access a real batch
request can't deliver. Exposed as its own small set of agent/MCP tools
(`dispatch_batch_completions`/`check_batch_status`/`get_batch_results`)
rather than folded into `bot.agent_runtime.subagents`.

Every function here talks to the real `anthropic` SDK's
`client.messages.batches` resource — confirmed live against the
installed package's actual method signatures and response-model field
names before writing this (`create`, `retrieve`, `results`; `MessageBatch`,
`MessageBatchIndividualResponse`, `MessageBatchSucceededResult`/
`...ErroredResult`/`...CanceledResult`/`...ExpiredResult`).
"""

from __future__ import annotations

import os
from typing import Any, Optional

MAX_BATCH_REQUESTS = 100_000  # Anthropic's own real per-batch cap


def _client():
    from anthropic import AsyncAnthropic
    from bot.backends.base import BackendError

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise BackendError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(api_key=api_key)


async def submit(
    tasks: list[dict[str, Any]], *, model: str, max_tokens: int = 4096, system_prompt: Optional[str] = None,
) -> str:
    """tasks: [{"custom_id": str, "goal": str}, ...] — each becomes one
    plain, single-turn (no tools) completion request in the batch.
    `custom_id` must be unique within the batch (Anthropic's own
    requirement) — the caller's job to make one, since it's what
    results() below uses to match a result back to its task. Returns the
    real batch id (e.g. "msgbatch_...")."""
    from bot.backends.base import BackendError

    if not tasks:
        raise BackendError("submit() needs at least one task")
    if len(tasks) > MAX_BATCH_REQUESTS:
        raise BackendError(f"batch of {len(tasks)} exceeds Anthropic's own {MAX_BATCH_REQUESTS}-request cap")

    requests = []
    for task in tasks:
        params: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": task["goal"]}],
        }
        if system_prompt:
            params["system"] = system_prompt
        requests.append({"custom_id": task["custom_id"], "params": params})

    client = _client()
    try:
        batch = await client.messages.batches.create(requests=requests)
    except Exception as exc:
        raise BackendError(f"batch submission failed: {exc}") from exc
    return batch.id


async def status(batch_id: str) -> dict[str, Any]:
    """{"id", "processing_status", "request_counts": {"processing",
    "succeeded", "errored", "canceled", "expired"}} — processing_status
    is "in_progress" | "canceling" | "ended"; poll until "ended" before
    calling results()."""
    from bot.backends.base import BackendError

    client = _client()
    try:
        batch = await client.messages.batches.retrieve(batch_id)
    except Exception as exc:
        raise BackendError(f"batch status lookup failed: {exc}") from exc
    counts = batch.request_counts
    return {
        "id": batch.id,
        "processing_status": batch.processing_status,
        "request_counts": {
            "processing": counts.processing, "succeeded": counts.succeeded,
            "errored": counts.errored, "canceled": counts.canceled, "expired": counts.expired,
        },
    }


async def results(batch_id: str) -> list[dict[str, Any]]:
    """[{"custom_id", "status": "succeeded"|"errored"|"canceled"|"expired",
    "text": Optional[str], "error": Optional[str]}, ...] — call only once
    status()'s processing_status is "ended"; Anthropic streams results as
    they finish, but this waits for the whole JSONL stream to close out
    (real bulk work, not something a caller needs mid-stream access to)."""
    from bot.backends.base import BackendError

    client = _client()
    out: list[dict[str, Any]] = []
    try:
        async for item in await client.messages.batches.results(batch_id):
            entry: dict[str, Any] = {"custom_id": item.custom_id, "status": item.result.type}
            if item.result.type == "succeeded":
                text = "".join(b.text for b in item.result.message.content if getattr(b, "type", "") == "text")
                entry["text"] = text
                entry["error"] = None
            elif item.result.type == "errored":
                entry["text"] = None
                entry["error"] = str(item.result.error)
            else:  # "canceled" or "expired" — no message, no error object
                entry["text"] = None
                entry["error"] = None
            out.append(entry)
    except Exception as exc:
        raise BackendError(f"fetching batch results failed: {exc}") from exc
    return out
