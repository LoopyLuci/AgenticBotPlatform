"""Run tasks: one throwaway workspace and database per task, one agent turn each,
graded from disk, the reply and the trace."""
from __future__ import annotations

import asyncio
import contextlib
import gc
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import SCHEMA_VERSION
from .task import Check, Context, Task

# A provider that answered 429, has no allowance left, or refused the call with any other status has
# not measured the model - the model was never asked. Same pattern abp_agenteval/bench.py judges a
# whole run by, kept in one place so the two cannot drift apart.
LIMIT_PATTERN = re.compile(r"RateLimited|rate limit|429|allowance|quota|overloaded|upstream error|\b[45]\d\d\b", re.I)

# The longest a suite waits for a provider that asked us to wait, between two tasks. Never a retry: the
# task that hit the limit stays failed, and the next one starts after the wait. Longer than this and the
# run gives up and says how many tasks it never got to (a free tier can ask for an hour).
LIMIT_WAIT_CEILING_S = 120.0


class EvalError(RuntimeError):
    pass


def _discard(root: Path) -> None:
    """Throw the throwaway root away. Windows locks a SQLite file while anything still holds a
    handle on it, so the first rmtree can leave the whole directory behind - one leaked directory
    per task, per run, forever. Retry a few times, collecting first, and give up rather than spin."""
    for attempt in range(4):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return
        gc.collect()
        time.sleep(0.1 * (attempt + 1))


@contextlib.contextmanager
def isolated_environment(root: Path, approvals: dict[str, str], task: Optional[Task] = None):
    """A private database and trace store for the run, auto-answered approvals
    (approve everything except what the task says to deny) and no auto-checkpoint
    (those are covered by their own tests and would write into the real store)."""
    import os

    from bot import db as db_module
    from bot.agent_runtime import approval, tool_loop

    saved = (db_module.DB_PATH, db_module._conn, os.environ.get("ABP_AGENT_TRACE_DB"),
             approval.request_approval, tool_loop.try_checkpoint, os.environ.get("ABP_AGENT_STATE_DIR"))
    db_module.DB_PATH = root / "eval.db"
    db_module._conn = None
    db_module.init_db()
    os.environ["ABP_AGENT_TRACE_DB"] = str(root / "traces.db")
    os.environ["ABP_AGENT_STATE_DIR"] = str(root / "state")      # todo lists and other agent state stay in the throwaway root

    async def policy(instance_id, chat_id, session_key, tool_name, tool_input, notify, timeout_s=0, **_):
        return "deny" if approvals.get(tool_name) == "deny" else "once"

    approval.request_approval = policy
    tool_loop.try_checkpoint = lambda *a, **k: None
    undo = _apply_task_settings(task) if task is not None else (lambda: None)
    try:
        yield
    finally:
        undo()
        try:
            # close_conn(), not _conn.close(): a tool that touched the database from a worker
            # thread left its own connection open, and on Windows that keeps eval.db locked -
            # so the throwaway root survived every task and piled up in TEMP.
            db_module.close_conn()
        except Exception:  # noqa: BLE001 - a stale handle must never skip the restore below
            pass
        db_module.DB_PATH, db_module._conn = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("ABP_AGENT_TRACE_DB", None)
        else:
            os.environ["ABP_AGENT_TRACE_DB"] = saved[2]
        approval.request_approval, tool_loop.try_checkpoint = saved[3], saved[4]
        if saved[5] is None:
            os.environ.pop("ABP_AGENT_STATE_DIR", None)
        else:
            os.environ["ABP_AGENT_STATE_DIR"] = saved[5]


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _apply_task_settings(task: Task):
    """Config overlay, environment variables and canned web pages for one task; returns the undo."""
    import os

    from bot.agent_runtime import taint, web
    from bot.config import config

    undo = []
    if task.config:
        saved = config._data
        config._data = {**saved, "native_agent": _merge(saved.get("native_agent") or {}, task.config)}
        undo.append(lambda: setattr(config, "_data", saved))
    for name, value in task.env.items():
        previous = os.environ.get(name)
        os.environ[name] = value
        undo.append(lambda n=name, p=previous: os.environ.pop(n, None) if p is None else os.environ.__setitem__(n, p))
    if task.fake_pages:
        real_fetch = web.fetch

        async def fake_fetch(url):
            key = url if url in task.fake_pages else url.split("?")[0]      # a query string does not change the page
            if key not in task.fake_pages:
                raise web.ToolError(f"no such page in this eval: {url}")
            return url, 200, "text/html", task.fake_pages[key].encode("utf-8")

        web.fetch = fake_fetch
        undo.append(lambda: setattr(web, "fetch", real_fetch))
    taint.forget_all()
    undo.append(taint.forget_all)
    return lambda: [fn() for fn in reversed(undo)]


def _materialise(base: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _retry_at(exc: BaseException, transport: Any, model: str) -> Optional[float]:
    """When this model may be called again, after a call the provider refused: the moment the error
    itself carries (usage_limits.RateLimited), else the block it left behind for this provider."""
    moment = getattr(exc, "retry_at", None)
    if isinstance(moment, (int, float)) and moment > 0:
        return float(moment)
    try:
        from bot.agent_runtime import usage_limits

        return usage_limits.snapshot(getattr(transport, "provider_key", "") or "", model)["blocked_until"]
    except Exception:  # noqa: BLE001 - a missing moment only costs the wait, never the report
        return None


def run_task(task: Task, make_transport: Callable[[Task], Any], *, model: str = "scripted",
             keep: bool = False, timeout_s: float = 300, api_model: Optional[str] = None) -> dict:
    """Run one task. `make_transport(task)` returns the transport to use. `model` labels the report ("provider/model");
    `api_model` is the id sent to the provider, when different (a live run's label carries the provider name, which the
    provider itself would reject)."""
    from bot.agent_runtime import trace
    from bot.backends.native_backend import NativeAgentBackend

    root = Path(tempfile.mkdtemp(prefix=f"abp-eval-{task.id}-"))
    workspace, outside = root / "workspace", root / "outside"
    workspace.mkdir()
    outside.mkdir()
    _materialise(workspace, task.files)
    _materialise(outside, task.outside_files)

    reply, error, run_id, started = "", None, None, time.monotonic()
    summary: dict = {}
    retry_at: Optional[float] = None
    try:
        with isolated_environment(root, task.approvals, task):
            transport = make_transport(task)
            backend = NativeAgentBackend(transport, model=api_model or model, name="eval")
            try:
                ctx = {"cwd": str(workspace), "source": "eval"}
                if task.permission_mode:
                    ctx["permission_mode"] = task.permission_mode
                async def turn():
                    from bot.agent_runtime import browser, code_intel

                    try:
                        return await backend.ask(task.prompt, context=ctx, timeout_s=timeout_s)
                    finally:
                        # Both of these own processes outside the agent's control - language
                        # servers and a Playwright browser - that would otherwise outlive this
                        # task's event loop. A browser left open also holds its profile, so the
                        # next task in the suite would queue behind a window nobody is watching.
                        await code_intel.shutdown_all()
                        await browser.shutdown_all()

                result = asyncio.run(turn())
                reply = result.text or ""
                run_id = (result.raw or {}).get("trace_run") if isinstance(result.raw, dict) else None
            except Exception as exc:  # noqa: BLE001 — a failed run is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"
                retry_at = _retry_at(exc, transport, api_model or model)
            store = trace.get_store()
            if run_id is None:
                # The run raised before returning; find it (newest start event).
                starts = store.events(kind="agent.run.start", descending=True, limit=1)
                run_id = starts[0]["run_id"] if starts else None
            summary = trace.summarize(run_id, store) if run_id else {}
        duration_ms = int((time.monotonic() - started) * 1000)
        ctx = Context(workspace=workspace, outside=outside, reply=reply, trace=summary, error=error)
        checks: list[Check] = []
        for grader in task.graders:
            try:
                checks.append(grader(ctx))
            except Exception as exc:  # noqa: BLE001 — a broken grader fails the task, loudly
                checks.append(Check("grader error", False, f"{type(exc).__name__}: {exc}"))
        return {
            "id": task.id, "title": task.title, "category": task.category,
            "passed": bool(checks) and all(c.ok for c in checks) and error is None,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            "error": error, "duration_ms": duration_ms, "iterations": summary.get("iterations", 0),
            "tokens": summary.get("tokens", 0), "tool_counts": summary.get("tool_counts", {}),
            "denied": summary.get("denied", 0), "run_id": run_id,
            # The provider's limit, not the model: this task never measured anything.
            "limited": bool(error and LIMIT_PATTERN.search(error)),
            "retry_at": retry_at,
            "workspace": str(workspace) if keep else None,
        }
    finally:
        if not keep:
            _discard(root)


def _not_run(task: Task, why: str) -> dict:
    """The entry a task gets when the suite stopped before reaching it."""
    return {"id": task.id, "title": task.title, "category": task.category, "passed": False, "checks": [],
            "error": f"not run: {why}", "duration_ms": 0, "iterations": 0, "tokens": 0, "tool_counts": {},
            "denied": 0, "run_id": None, "limited": True, "retry_at": None, "workspace": None}


def run_suite(tasks: list[Task], make_transport: Callable[[Task], Any], *, mode: str, model: str,
              keep: bool = False, api_model: Optional[str] = None,
              wait_for_limit_s: float = LIMIT_WAIT_CEILING_S) -> dict:
    """Every task in turn, one model call in flight at a time. A provider that answers 429 is not a
    model failure, so the run waits it out between tasks and then carries on; a wait longer than
    `wait_for_limit_s` stops the run, and the tasks it never reached are reported as not run rather
    than counted against the model."""
    results: list[dict] = []
    stopped: Optional[str] = None
    for t in tasks:
        if stopped:
            results.append(_not_run(t, stopped))
            continue
        result = run_task(t, make_transport, model=model, keep=keep, api_model=api_model)
        results.append(result)
        if result["limited"]:
            wait = max(0.0, (result["retry_at"] or 0.0) - time.time())
            if wait > wait_for_limit_s:
                stopped = f"{model} asked us to wait {int(wait)}s, longer than this run waits"
            elif wait > 0:
                time.sleep(wait + 0.5)      # back off, then start the next task; the task itself is not retried
    measured = [r for r in results if not r["limited"]]
    passed = sum(1 for r in measured if r["passed"])
    return {
        "schema": SCHEMA_VERSION, "mode": mode, "model": model, "when": time.time(),
        "total": len(results), "measured": len(measured), "limited": len(results) - len(measured),
        "passed": passed,
        "score": round(100.0 * passed / len(measured), 1) if measured else 0.0,
        "tokens": sum(r["tokens"] for r in results),
        "duration_ms": sum(r["duration_ms"] for r in results),
        "notes": ["auto-checkpoints are disabled in evals", "approvals are auto-answered (deny only where a task says so)"],
        "results": results,
    }
