"""python -m abp_acp --model auto | provider/model | scripted:FILE

Speaks the Agent Client Protocol on standard input/output so an editor can use the ABP agent.
Point the editor's "custom agent" setting at:  python -m abp_acp --model auto

`--model auto` lets ABP's model router pick, once per session from its first prompt, the best
configured model for the task. Like a bot on `model: auto`, it never picks Claude unless you
listed it in `native_agent.router.candidates`. `scripted:FILE` replays a JSON list of steps
instead of calling a model, for testing an editor integration without a key or any cost:
`[{"call": "write_file", "args": {"path": "a.txt", "content": "hi"}}, {"say": "done"}]`.

Standard output carries only protocol messages; logs go to standard error. The agent asks the editor
before any tool call that needs approval (permission rules in config/backends.yaml still apply)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from abp_run import core

from .server import AcpServer, Session


class StdioLines:
    """Line reader and writer over the process's standard streams. Reading happens on a thread, because
    console and pipe handles cannot be awaited portably (Windows)."""

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: asyncio.Queue = asyncio.Queue()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        # Native libraries that touch the process's standard handles while they initialise (numpy, and OpenCV on top
        # of it) must load BEFORE the reader thread blocks in a synchronous read on stdin: on Windows, a second
        # operation on a handle waits behind a pending synchronous read, so a tool that first imports them mid-turn
        # would wait for input the client only sends after the reply. (It hung the pipeline's ACP tests.)
        for lib in ("numpy", "cv2"):
            try:
                __import__(lib)
            except ImportError:
                pass

        def pump() -> None:
            try:
                for line in self._in:
                    loop.call_soon_threadsafe(self._queue.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(self._queue.put_nowait, None)

        threading.Thread(target=pump, daemon=True, name="acp-stdin").start()

    async def read_line(self) -> Optional[str]:
        return await self._queue.get()

    async def write_line(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()


def scripted_transport(path: str):
    """A transport that replays the steps in a JSON file (see the module docstring)."""
    from abp_agenteval.scripted import ScriptedTransport
    from abp_agenteval.task import Call, Say

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise core.RunError(f"could not read the script {path}: {exc}") from exc
    steps = []
    for step in raw if isinstance(raw, list) else []:
        if isinstance(step, dict) and "say" in step:
            steps.append(Say(str(step["say"])))
        elif isinstance(step, dict) and "call" in step:
            steps.append(Call(str(step["call"]), dict(step.get("args") or {})))
        else:
            raise core.RunError(f"script step {step!r} is neither {{\"say\": ...}} nor {{\"call\": ..., \"args\": ...}}")
    return ScriptedTransport(steps)


def resolve_auto(task: str):
    """(transport, model, "provider/model") the router picks for `task`; never Claude unless configured."""
    from bot.backends.base import BackendError
    from bot.backends.native_backend import _resolve_auto_transport

    try:
        return _resolve_auto_transport(task, exclude=set())
    except BackendError as exc:
        raise core.RunError(str(exc)) from exc


def make_runner(model_ref: str, permission_mode: Optional[str]):
    """The per-turn runner. A fixed model is resolved now, so a bad name fails at startup;
    `auto` is resolved per session, from that session's first prompt."""
    fixed = None
    if model_ref.startswith("scripted:"):
        fixed = (scripted_transport(model_ref[len("scripted:"):]), "scripted", "scripted")
    elif model_ref != "auto":
        provider, model = core.split_model(model_ref)
        fixed = (core.transport_for(provider, model), model, f"{provider}/{model}")
    chosen: dict[str, tuple] = {}

    async def runner(prompt: str, session: Session, on_text, progress, approval_notify):
        if session.id not in chosen:
            chosen[session.id] = fixed or await asyncio.to_thread(resolve_auto, prompt)
        transport, model, ref = chosen[session.id]
        session.model = ref
        return await core.run_turn(
            prompt, transport=transport, model=model, cwd=session.cwd, permission_mode=permission_mode, on_text=on_text,
            timeout_s=3600, extra_context={"desktop_session_key": f"acp:{session.id}", "progress_notify": progress,
                                           "approval_notify": approval_notify, "chat_id": session.id, "instance_id": 0})

    return runner


async def amain(args) -> int:
    root = Path(tempfile.mkdtemp(prefix="abp-acp-"))
    lines = StdioLines()
    lines.start(asyncio.get_running_loop())
    with core.ephemeral_environment(root, "ask"):
        server = AcpServer(lines.read_line, lines.write_line, make_runner(args.model.strip(), args.permission_mode))
        try:
            await server.serve()
        finally:
            from bot.agent_runtime import code_intel

            await code_intel.shutdown_all()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_acp", description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=os.environ.get("ABP_RUN_MODEL", ""))
    ap.add_argument("--permission-mode", choices=["plan", "default", "accept_edits", "bypass"], default=None)
    args = ap.parse_args(argv)
    if not args.model:
        print("--model is required: auto, provider/model or scripted:FILE (or set ABP_RUN_MODEL)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(amain(args))
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
