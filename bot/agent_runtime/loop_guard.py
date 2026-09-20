"""Keeping the agent loop honest: limits, going-in-circles detection, and running
independent tool calls side by side (roadmap P1).

Limits are configurable under `native_agent.limits` (0 turns a limit off):

    native_agent:
      limits:
        max_iterations: 30      # model calls in one turn
        max_seconds: 0          # wall-clock time for one turn
        max_tokens: 0           # tokens spent in one turn

Reaching a limit is not an error. The loop asks the model for a short summary of what
is done and what remains, returns that as the reply, and the session keeps everything,
so "continue" picks up where it stopped.

The watchdog notices two patterns and says so to the model, then stops the turn if it
persists: the same call repeated with the same result, and several rounds in a row in
which every tool call failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

DEFAULT_MAX_ITERATIONS = 30
REPEAT_WARN = 3
REPEAT_STOP = 5
FAILURE_WARN = 3
FAILURE_STOP = 6
MAX_PARALLEL = 8
WINDOW = 8


@dataclass(frozen=True)
class Limits:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_seconds: float = 0
    max_tokens: int = 0


def limits() -> Limits:
    try:
        from bot.config import config

        cfg = ((config.current.get("native_agent") or {}).get("limits")) or {}
    except Exception:  # noqa: BLE001
        cfg = {}

    def num(key, default, cast):
        try:
            value = cast(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(0, value)

    return Limits(num("max_iterations", DEFAULT_MAX_ITERATIONS, int), num("max_seconds", 0, float),
                  num("max_tokens", 0, int))


def _failed(output: str) -> bool:
    head = (output or "")[:80]
    return head.startswith("Error:") or head.startswith("Denied")


class Watchdog:
    def __init__(self, lim: Limits, *, clock: Callable[[], float] = time.monotonic):
        self.limits = lim
        self._clock = clock
        self._started = clock()
        self.iterations = 0
        self._recent: list[tuple[str, str]] = []
        self._failure_streak = 0
        self.stop_reason: Optional[str] = None

    def before_call(self, total_tokens: int) -> Optional[str]:
        """Called before each model call. Returns why the turn must stop, or None."""
        if self.stop_reason:
            return self.stop_reason
        lim = self.limits
        reason = None
        if lim.max_iterations and self.iterations >= lim.max_iterations:
            reason = f"it reached the limit of {lim.max_iterations} steps for one turn"
        elif lim.max_seconds and self._clock() - self._started >= lim.max_seconds:
            reason = f"it reached the time limit of {int(lim.max_seconds)} seconds for one turn"
        elif lim.max_tokens and total_tokens >= lim.max_tokens:
            reason = f"it reached the limit of {lim.max_tokens} tokens for one turn"
        if reason:
            self.stop_reason = reason
            return reason
        self.iterations += 1
        return None

    def after_round(self, results: list) -> list[str]:
        """Called with a round's (tool_call, output) pairs. Returns one note per result ('' for none)
        to append to that result, and may set stop_reason."""
        notes = [""] * len(results)
        for i, (tc, output) in enumerate(results):
            sig = hashlib.sha1(json.dumps([tc.name, tc.arguments], sort_keys=True, default=str).encode()).hexdigest()
            out = hashlib.sha1((output or "").encode("utf-8", "replace")).hexdigest()
            self._recent.append((sig, out))
            del self._recent[:-WINDOW]
            same = sum(1 for s in self._recent if s == (sig, out))
            if same >= REPEAT_STOP:
                self.stop_reason = f"it kept repeating the same {tc.name} call with the same result"
            elif same >= REPEAT_WARN:
                notes[i] = (f"\n[Note: this is the {same}th time you made this exact call and got this exact result. "
                            "Try something different, or say what is blocking you.]")
        if results and all(_failed(o) for _, o in results):
            self._failure_streak += 1
        else:
            self._failure_streak = 0
        if self._failure_streak >= FAILURE_STOP:
            self.stop_reason = "every tool call kept failing"
        elif self._failure_streak >= FAILURE_WARN and results:
            tc, output = results[-1]
            notes[-1] += (f"\n[Note: the last {self._failure_streak} rounds of tool calls all failed. "
                          "Re-read the errors and change your approach.]")
        return notes


async def run_calls(calls: list, run_one: Callable[[object], Awaitable[str]], is_safe: Callable[[str], bool],
                    results: list, *, max_parallel: int = MAX_PARALLEL) -> None:
    """Run tool calls in order, except that a run of consecutive read-only calls executes together.
    Appends (call, output) to `results` in the model's order as each group finishes, so a caller
    that is cancelled can see what completed."""
    i = 0
    while i < len(calls):
        if is_safe(calls[i].name):
            j = i
            while j < len(calls) and is_safe(calls[j].name):
                j += 1
            group = calls[i:j]
            if len(group) > 1:
                gate = asyncio.Semaphore(max_parallel)

                async def one(tc):
                    async with gate:
                        return await run_one(tc)

                outputs = await asyncio.gather(*(one(tc) for tc in group))
                results.extend(zip(group, outputs))
                i = j
                continue
        results.append((calls[i], await run_one(calls[i])))
        i += 1
