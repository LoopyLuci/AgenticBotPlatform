"""python -m abp_acp --model provider/model [--cwd-default DIR]

Speaks the Agent Client Protocol on standard input/output so an editor can use the ABP agent.
Point the editor's "custom agent" setting at:  python -m abp_acp --model anthropic/claude-sonnet-5

Standard output carries only protocol messages; logs go to standard error. The agent asks the editor
before any tool call that needs approval (permission rules in config/backends.yaml still apply)."""
from __future__ import annotations

import argparse
import asyncio
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


def make_runner(provider: str, model: str, permission_mode: Optional[str]):
    transport = core.transport_for(provider, model)

    async def runner(prompt: str, session: Session, on_text, progress, approval_notify):
        return await core.run_turn(
            prompt, transport=transport, model=model, cwd=session.cwd, permission_mode=permission_mode, on_text=on_text,
            timeout_s=3600, extra_context={"desktop_session_key": f"acp:{session.id}", "progress_notify": progress,
                                           "approval_notify": approval_notify, "chat_id": session.id, "instance_id": 0})

    return runner


async def amain(args) -> int:
    provider, model = core.split_model(args.model)
    root = Path(tempfile.mkdtemp(prefix="abp-acp-"))
    lines = StdioLines()
    lines.start(asyncio.get_running_loop())
    with core.ephemeral_environment(root, "ask"):
        server = AcpServer(lines.read_line, lines.write_line, make_runner(provider, model, args.permission_mode))
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
        print("--model provider/model is required (or set ABP_RUN_MODEL)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(amain(args))
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
