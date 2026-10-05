"""Hermes Agent CLI backend — shells out to `hermes -z "<prompt>"`.

Mirrors bot/backends/cli_backend.py's shape closely (same spawn/timeout/kill
pattern, one sandbox_ns cell per call - see bot/backends/base.py's
process_cell) since Hermes's one-shot mode is the same kind of
integration as Claude Code CLI's headless print mode: no persistent
process, one prompt in, one answer out per call.

Confirmed live against the real `hermes` CLI (not guessed from docs):
- `hermes -z "<prompt>" --usage-file <path>` writes the plain final answer
  text to stdout (exit 0 on success, non-zero on failure) and, after
  completion, a JSON usage report to --usage-file — unlike Claude Code
  CLI's `--output-format json`, stdout here is *not* JSON, it's just the
  answer. That usage report includes a `session_id` field even though
  nothing is printed to stdout about it (`--pass-session-id` only puts the
  id in the *model's own* system prompt, so it isn't a way for this code to
  learn it — the usage file already is).
- `hermes --resume <session_id> -z "<prompt>"` genuinely continues that
  exact session (confirmed by asking a follow-up question that only makes
  sense with the first turn's context, and getting the right answer back).
- `-m/--model MODEL` and `--reasoning LEVEL` (accepting exactly ABP's own
  effort ladder: none/minimal/low/medium/high/xhigh/max/ultra — see
  bot/effort.py's comment on why that ladder mirrors Hermes's own) both
  apply to `-z`/`--oneshot` per `hermes --help`.
- Session/workspace scoping in Hermes is tied to the invoking process's
  cwd ("-c 'name' … the most recent **in lineage**" per its own --help);
  this backend does not set a per-call cwd, so every call runs from ABP's
  own server directory and stays in one consistent scope — there is no
  per-instance workspace isolation the way hermes_gateway_backend's
  `hermes_home` gives the gateway backend.

So despite having no long-lived process, this is NOT stateless: passing
back the `session_id` from --usage-file as `raw["desktop_session_key"]`
lets bot/router.py persist it exactly the way hermes_gateway_backend.py's
richer session protocol does (Router.ask()'s shared "any backend that
returns raw['desktop_session_key'] gets it linked to the chat" path — see
router.py around its `result.raw.get("desktop_session_key")` check), so a
bot instance using hermes_cli keeps one real, continuous Hermes
conversation across calls instead of a fresh one each time.

bot/backends/hermes_gateway_backend.py is the richer alternative when
async/streaming or per-instance workspace isolation matter more than this
backend's own simplicity (no server process to keep running).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from bot.backends.base import Backend, BackendError, BackendResult, process_cell
from bot.sandbox_ns.spawn import async_spawn

logger = logging.getLogger("bot.backends.hermes_cli")


class HermesCliBackend(Backend):
    name = "hermes_cli"

    def __init__(self, binary: str = "hermes", extra_args: Optional[list[str]] = None, model: Optional[str] = None,
                 env: Optional[dict[str, str]] = None):
        self.binary = binary
        self.extra_args = extra_args or []
        self.model = model
        # Per-call environment overlay, handed straight to the child instead of
        # being written into os.environ: a caller that scopes HERMES_HOME to one
        # home (bot/hermes_gateway.py's `ask`) must not mutate this process's
        # environment to do it, or two concurrent calls would swap homes under
        # each other. None keeps the child's environment untouched (it then
        # simply inherits ours), exactly as before.
        self.env = env

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 60) -> BackendResult:
        from bot import effort as effort_mod

        context = context or {}
        fd, usage_path = tempfile.mkstemp(prefix="hermes_usage_", suffix=".json")
        os.close(fd)
        usage_file = Path(usage_path)

        model_args = ["--model", self.model] if self.model else []
        session = context.get("desktop_session_key")
        resume_args = ["--resume", str(session)] if session else []
        reasoning = effort_mod.to_hermes(context.get("effort"))
        reasoning_args = ["--reasoning", reasoning] if reasoning else []
        args = [self.binary, "-z", prompt, "--usage-file", str(usage_file), *model_args, *resume_args, *reasoning_args, *self.extra_args]

        # One cell per call (preset "agent"): the timeout and the /stop below stop the CLI and
        # whatever it started, which a bare proc.kill() never did. See bot/backends/base.py.
        cell = process_cell(f"hermes_cli {self.binary}", owner="backends.hermes_cli")
        try:
            proc = await async_spawn(
                args,
                cell=cell,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                name=f"hermes_cli {self.binary}",
                owner="backends.hermes_cli",
            )
        except FileNotFoundError as exc:
            cell.close()
            usage_file.unlink(missing_ok=True)
            raise BackendError(f"'{self.binary}' not found on PATH — is Hermes Agent installed?") from exc

        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                cell.kill(f"`{self.binary}` passed its {timeout_s:g}s timeout")
                await proc.wait()
                raise BackendError(f"hermes_cli backend timed out after {timeout_s}s") from exc
            except asyncio.CancelledError:
                # /stop cancelling the task wrapping this call — see the
                # matching comment in cli_backend.py.
                cell.kill("the run was cancelled")
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
            if isinstance(usage_raw, dict) and usage_raw.get("session_id"):
                # Router.ask() persists this against the bot instance/chat so the NEXT call resumes the same
                # real Hermes session instead of starting over — see this module's docstring.
                usage_raw = {**usage_raw, "desktop_session_key": usage_raw["session_id"]}
            return BackendResult(text=text, tokens=tokens, raw=usage_raw)
        finally:
            usage_file.unlink(missing_ok=True)
            cell.close()          # the call is over either way: release its job handle

    def __repr__(self) -> str:
        return f"HermesCliBackend(binary={self.binary!r})"
