"""Run one agent turn without a chat channel (roadmap P5). Used by the `abp_run` command line, the
editor protocol server (abp_acp) and CI jobs.

Nothing here talks to a chat platform. By default the run is **ephemeral**: its own throwaway database
and trace store, so a CI job leaves nothing behind and never touches a bot's history."""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LIMITED, EXIT_RATE = 0, 1, 2, 3, 4


class RunError(Exception):
    """Something that stops a run before the model is reached (unknown provider, bad option)."""


@dataclass
class RunResult:
    ok: bool
    reply: str = ""
    error: str = ""
    model: str = ""
    tokens: int = 0
    duration_ms: int = 0
    iterations: int = 0
    tool_calls: list = field(default_factory=list)
    denied: int = 0
    run_id: str = ""
    status: str = ""
    exit_code: int = EXIT_OK
    stopped: str = ""          # why the turn was cut short (step / time / token limit), if it was

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("ok", "reply", "error", "model", "tokens", "duration_ms", "iterations",
                                              "tool_calls", "denied", "run_id", "status", "exit_code", "stopped")}


def transport_for(provider: str, model: str):
    """The model transport for `provider` ("anthropic", or a name in config/providers.yaml)."""
    if provider in ("", "anthropic"):
        from bot.agent_runtime.transports.anthropic import AnthropicTransport

        return AnthropicTransport()
    from bot import providers as registry
    from bot.agent_runtime.transports import build_openai_transport

    cfg = registry.get_provider(provider)
    if cfg is None:
        raise RunError(f"no provider named {provider!r} in config/providers.yaml (and it is not 'anthropic')")
    return build_openai_transport(protocol=cfg.get("protocol", "openai"), base_url=cfg["base_url"],
                                  api_key=registry.get_api_key(provider), catalog_id=cfg.get("catalog_id"))


def split_model(ref: str, default_provider: str = "anthropic") -> tuple[str, str]:
    """"provider/model" -> (provider, model); a bare model uses `default_provider`."""
    ref = (ref or "").strip()
    if not ref:
        raise RunError("no model given (use --model provider/model, or set ABP_RUN_MODEL)")
    if "/" in ref:
        head, _, tail = ref.partition("/")
        if head == "anthropic" or head in _providers():
            return head, tail
    return default_provider, ref


def _providers() -> set[str]:
    try:
        from bot import providers as registry

        return set(registry.list_providers())
    except Exception:  # noqa: BLE001
        return set()


@contextlib.contextmanager
def ephemeral_environment(root: Path, approve: str):
    """A private database, trace store and state folder, and approvals answered by policy instead of a person.
    `approve` is "deny" (anything that needs approval is refused - the safe default for CI), "allow", or "ask"
    (leave approvals to whoever supplies `approval_notify`, as an editor does)."""
    from bot import db as db_module
    from bot.agent_runtime import approval, tool_loop

    saved = (db_module.DB_PATH, db_module._conn, os.environ.get("ABP_AGENT_TRACE_DB"), os.environ.get("ABP_AGENT_STATE_DIR"),
             approval.request_approval, tool_loop.try_checkpoint)
    db_module.DB_PATH, db_module._conn = root / "run.db", None
    db_module.init_db()
    os.environ["ABP_AGENT_TRACE_DB"] = str(root / "traces.db")
    os.environ["ABP_AGENT_STATE_DIR"] = str(root / "state")

    async def policy(instance_id, chat_id, session_key, tool_name, tool_input, notify, timeout_s=0, force=False, **_):
        # A session that read untrusted content forces a real approval (force=True); nobody is here to give one.
        return "once" if approve == "allow" and not force else "deny"

    if approve != "ask":
        approval.request_approval = policy
    tool_loop.try_checkpoint = lambda *a, **k: None
    try:
        yield
    finally:
        try:
            if db_module._conn is not None:
                db_module._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db_module.DB_PATH, db_module._conn = saved[0], saved[1]
        for key, value in (("ABP_AGENT_TRACE_DB", saved[2]), ("ABP_AGENT_STATE_DIR", saved[3])):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        approval.request_approval, tool_loop.try_checkpoint = saved[4], saved[5]


async def run_turn(prompt: str, *, transport, model: str, cwd: Path, permission_mode: Optional[str] = None,
                   on_text: Optional[Callable[[str], Awaitable[None]]] = None, timeout_s: float = 600,
                   extra_context: Optional[dict] = None) -> RunResult:
    """One agent turn against `transport`. Must be called inside `ephemeral_environment` (or with a real database)."""
    from bot.agent_runtime import trace
    from bot.backends.base import BackendError
    from bot.backends.native_backend import NativeAgentBackend

    backend = NativeAgentBackend(transport, model=model, name="run", session_prefix="run")
    ctx: dict[str, Any] = {"cwd": str(cwd), "source": "headless", **(extra_context or {})}
    if permission_mode:
        ctx["permission_mode"] = permission_mode
    if on_text is not None:
        from bot.agent_runtime.transports.base import StreamEvent

        async def notify(event: StreamEvent) -> None:
            if event.kind == "text" and event.text:
                await on_text(event.text)

        ctx["stream_notify"] = notify
    started = time.monotonic()
    result = RunResult(ok=False, model=model)
    run_id = None
    try:
        answer = await backend.ask(prompt, context=ctx, timeout_s=timeout_s)
        result.reply, result.ok = answer.text or "", True
        raw = answer.raw if isinstance(answer.raw, dict) else {}
        run_id, result.stopped = raw.get("trace_run"), str(raw.get("stopped") or "")
        if result.stopped:
            result.exit_code = EXIT_LIMITED
    except asyncio.CancelledError:
        raise
    except BackendError as exc:
        result.error = str(exc)
        from bot.agent_runtime.usage_limits import RateLimited

        result.exit_code = EXIT_RATE if isinstance(exc, RateLimited) or " 429" in str(exc) else EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a failed run is a result, not a crash
        result.error, result.exit_code = f"{type(exc).__name__}: {exc}", EXIT_FAILED
    store = trace.get_store()
    if run_id is None:
        starts = store.events(kind="agent.run.start", descending=True, limit=1)
        run_id = starts[0]["run_id"] if starts else None
    if run_id:
        summary = trace.summarize(run_id, store)
        result.run_id, result.status = run_id, summary.get("status", "")
        result.tokens, result.iterations = int(summary.get("tokens") or 0), int(summary.get("iterations") or 0)
        result.tool_calls, result.denied = summary.get("tool_calls", []), int(summary.get("denied") or 0)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def run_once(prompt: str, *, provider: str, model: str, cwd: Path, approve: str = "deny", permission_mode: Optional[str] = None,
             on_text: Optional[Callable[[str], Awaitable[None]]] = None, timeout_s: float = 600, transport=None,
             persist: bool = False, extra_context: Optional[dict] = None) -> RunResult:
    """Everything for a one-shot run: build the transport, isolate the environment, run, clean up."""
    transport = transport or transport_for(provider, model)
    root = Path(tempfile.mkdtemp(prefix="abp-run-"))
    try:
        async def turn() -> RunResult:
            from bot.agent_runtime import code_intel

            try:
                return await run_turn(prompt, transport=transport, model=model, cwd=cwd, permission_mode=permission_mode,
                                      on_text=on_text, timeout_s=timeout_s, extra_context=extra_context)
            finally:
                await code_intel.shutdown_all()            # language servers must not outlive this run's event loop

        if persist:
            return asyncio.run(turn())
        with ephemeral_environment(root, approve):
            return asyncio.run(turn())
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
