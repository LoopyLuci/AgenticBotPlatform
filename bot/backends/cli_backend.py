"""Claude Code CLI backend — shells out to `claude` in headless print mode.

Scriptable and reliable: real exit codes, structured JSON output, no GUI
dependency. Tool permissions default to empty (no file/shell access) for
prompts relayed from Telegram, per the security model in the design spec —
widen `allowed_tools` in config/backends.yaml deliberately, per action type,
if you want a chat-originated prompt to be able to touch files.

Flag names (--output-format, --allowedTools) match Claude Code CLI as of
this writing; if your installed version differs, adjust `extra_args` in
config/backends.yaml rather than editing this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Optional

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.cli")


class CliBackend(Backend):
    name = "cli"

    def __init__(
        self,
        binary: str = "claude",
        allowed_tools: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ):
        self.binary = binary
        self.allowed_tools = allowed_tools or []
        self.cwd = cwd
        self.extra_args = extra_args or []

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 60) -> BackendResult:
        args = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        else:
            args += ["--allowedTools", ""]
        args += self.extra_args

        cwd = (context or {}).get("cwd") or self.cwd

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BackendError(
                f"'{self.binary}' not found on PATH — is Claude Code CLI installed?"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BackendError(f"cli backend timed out after {timeout_s}s") from exc
        except asyncio.CancelledError:
            # /stop cancelling the task wrapping this call — without this,
            # the subprocess would keep running orphaned after we stop
            # waiting on it, which defeats the point of /stop.
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            if not detail:
                # The CLI often reports the real error (auth failures, etc.)
                # as JSON on stdout rather than stderr, even on nonzero exit.
                try:
                    data = json.loads(stdout.decode(errors="replace"))
                    detail = data.get("result") or ""
                except json.JSONDecodeError:
                    pass
            raise BackendError(f"cli exited {proc.returncode}: {detail[:500]}")

        raw_text = stdout.decode(errors="replace")
        try:
            data = json.loads(raw_text)
            text = data.get("result") or data.get("output") or raw_text
            tokens = None
            usage = data.get("usage") or {}
            if usage:
                tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
            return BackendResult(text=text, tokens=tokens, raw=data)
        except json.JSONDecodeError:
            return BackendResult(text=raw_text, tokens=None, raw=raw_text)

    def __repr__(self) -> str:
        return f"CliBackend(binary={self.binary!r}, allowed_tools={self.allowed_tools!r})"
