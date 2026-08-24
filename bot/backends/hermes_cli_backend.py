"""Hermes Agent CLI backend — shells out to `hermes -z "<prompt>"`.

Mirrors bot/backends/cli_backend.py's shape closely (same subprocess/
timeout/kill pattern) since Hermes's one-shot mode is the same kind of
integration as Claude Code CLI's headless print mode: no persistent
process, no session state, one prompt in, one answer out.

Confirmed live against the real `hermes` CLI (not guessed from docs):
`hermes -z "<prompt>" --usage-file <path>` writes the plain final answer
text to stdout (exit 0 on success, non-zero on failure) and, after
completion, a JSON usage report to --usage-file with a `total_tokens`
field — unlike Claude Code CLI's `--output-format json`, stdout here is
*not* JSON, it's just the answer.

This is the "no server needed" Hermes integration — bot/backends/
hermes_gateway_backend.py is the richer, session-based alternative for
when async/streaming matters more than simplicity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.hermes_cli")


class HermesCliBackend(Backend):
    name = "hermes_cli"

    def __init__(self, binary: str = "hermes", extra_args: Optional[list[str]] = None, model: Optional[str] = None):
        self.binary = binary
        self.extra_args = extra_args or []
        self.model = model

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 60) -> BackendResult:
        fd, usage_path = tempfile.mkstemp(prefix="hermes_usage_", suffix=".json")
        os.close(fd)
        usage_file = Path(usage_path)

        # --model's exact flag name is unverified against a real `hermes`
        # install (this codebase has no live Hermes CLI to confirm it
        # against) — if `hermes` rejects it, correct this to whatever
        # `hermes --help` actually documents.
        model_args = ["--model", self.model] if self.model else []
        args = [self.binary, "-z", prompt, "--usage-file", str(usage_file), *model_args, *self.extra_args]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            usage_file.unlink(missing_ok=True)
            raise BackendError(f"'{self.binary}' not found on PATH — is Hermes Agent installed?") from exc

        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise BackendError(f"hermes_cli backend timed out after {timeout_s}s") from exc
            except asyncio.CancelledError:
                # /stop cancelling the task wrapping this call — see the
                # matching comment in cli_backend.py.
                proc.kill()
                await proc.wait()
                raise

            if proc.returncode != 0:
                raise BackendError(
                    f"hermes exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
                )

            text = stdout.decode(errors="replace").strip()
            # Confirmed live: when Hermes's own underlying API call fails
            # (rate limits, provider outages), `hermes -z` still exits 0 and
            # prints a plain-English failure message to stdout instead of
            # the answer — so returncode alone isn't a reliable success
            # signal. Without this, jobs land in the DB as status=success
            # with an error message as their "result", which is what a
            # caller (a swarm run, ask_instance, a Telegram reply) would
            # then treat as the real answer.
            if text.startswith("API call failed"):
                raise BackendError(f"hermes reported a failure: {text[:500]}")
            tokens = None
            usage_raw = None
            if usage_file.exists():
                try:
                    usage_raw = json.loads(usage_file.read_text(encoding="utf-8"))
                    tokens = usage_raw.get("total_tokens")
                except (json.JSONDecodeError, OSError):
                    pass
            return BackendResult(text=text, tokens=tokens, raw=usage_raw)
        finally:
            usage_file.unlink(missing_ok=True)

    def __repr__(self) -> str:
        return f"HermesCliBackend(binary={self.binary!r})"
