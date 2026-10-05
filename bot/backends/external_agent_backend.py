"""Delegating backends: hand a turn to another agent product's command line and return its answer (roadmap P5).

    backends:
      opencode:  {binary: opencode, model: null, agent: null, auto_approve: false, extra_args: []}
      openclaw:  {binary: openclaw, model: null, agent: null, extra_args: []}

The point is choice: a bot instance can use OpenCode or OpenClaw as its engine (with their own tools, models
and permission handling) while keeping ABP's channels, pairing, schedules and dashboards around it. ABP's own
tool loop, permission rules, taint tracking and traces do **not** apply inside these agents - what they may do
is decided by their own configuration. That is the trade, and the reason each is a separate backend name
rather than something a user could pick by accident.

Both run as a subprocess without a shell, in the turn's working folder, with a timeout, and are killed with
their process tree when the turn is cancelled - each turn in its own sandbox_ns cell (preset "agent"), so that
kill is the cell's (a Win32 Job Object, or a session of its own) rather than a tree walk that can miss a
process the agent forked.

The command lines follow each product's own documentation for scripted use (opencode.ai/docs/cli,
docs.openclaw.ai/cli/agent). OpenCode's own `run --help` and `--session`/`--format json` flags were confirmed live
against a real, installed `opencode` 1.18.32 - including that a failing call (a provider/credits/privacy-setting
error) still exits 0 and reports the failure as a `{"type":"error",...}` JSON event rather than a non-zero status,
and that every event (success or error) carries a real `sessionID` this backend now captures and hands back as
`raw["desktop_session_key"]` for bot/router.py to persist, the same way hermes_gateway_backend.py's session
protocol does, so a bot instance using `opencode` keeps one continuous conversation across calls. What was **not**
observed live: a successful reply's exact JSON shape (every live call this was tested with hit a credits/privacy
error before producing one) - `_find_text()`'s generic best-effort search is used for that, not a hard-coded field
name. OpenClaw is **not tested against a real install** (not installed where this was written) - only against a
stand-in executable that records its arguments; its own JSON envelope is searched for its text field the same way,
and if a flag differs in your version, put the correct one in `extra_args`.
(falling back to the raw output).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, Optional

from bot.backends.base import Backend, BackendError, BackendResult, process_cell
from bot.sandbox_ns.spawn import async_spawn

logger = logging.getLogger("bot.backends.external")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_TEXT_KEYS = ("text", "finalText", "final_text", "reply", "response", "message", "output", "content", "result")
MAX_OUTPUT_CHARS = 200_000


def _find_text(data: Any, depth: int = 0) -> Optional[str]:
    """The reply text inside a JSON envelope: the first non-empty string under a text-like key, searching
    nested objects (and the last element of lists) breadth-first."""
    if depth > 4:
        return None
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, list):
        for item in reversed(data):
            found = _find_text(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(data, dict):
        for key in _TEXT_KEYS:
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
        for key in _TEXT_KEYS:
            if isinstance(data.get(key), (dict, list)):
                found = _find_text(data[key], depth + 1)
                if found:
                    return found
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = _find_text(value, depth + 1)
                if found:
                    return found
    return None


class ExternalAgentBackend(Backend):
    preset = "external"

    def __init__(self, binary: str, *, model: Optional[str] = None, agent: Optional[str] = None,
                 extra_args: Optional[list[str]] = None, cwd: Optional[str] = None, auto_approve: bool = False):
        self.binary, self.model, self.agent = binary, model, agent
        self.extra_args = [str(a) for a in (extra_args or [])]
        self.cwd, self.auto_approve = cwd, auto_approve

    # ---- per product ------------------------------------------------------------------------
    def build_args(self, prompt: str, *, cwd: Optional[str], session: Optional[str], timeout_s: float) -> list[str]:
        raise NotImplementedError

    def parse_output(self, stdout: str) -> str:
        return _ANSI.sub("", stdout).strip()

    # ---- running --------------------------------------------------------------------------------
    async def ask(self, prompt: str, *, context: Optional[dict] = None, timeout_s: float = 300) -> BackendResult:
        exe = shutil.which(self.binary) or (self.binary if os.path.isfile(self.binary) else None)
        if exe is None:
            raise BackendError(f"{self.binary!r} was not found on PATH - is {self.name} installed?")
        context = context or {}
        cwd = context.get("cwd") or self.cwd
        session = context.get("desktop_session_key") or context.get("session_key")
        args = [exe, *self.build_args(prompt, cwd=cwd, session=str(session) if session else None, timeout_s=timeout_s), *self.extra_args]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            import subprocess

            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        # One cell per turn (preset "agent"), so the tree kill _kill_tree() does below is the cell's
        # own - a Win32 Job Object on Windows, a session of its own elsewhere - and the run shows up
        # on the diagnostics page. See bot/backends/base.py's process_cell.
        cell = process_cell(f"{self.name} {self.binary}", owner="backends.external")
        try:
            proc = await async_spawn(args, cell=cell, cwd=cwd or None, stdin=asyncio.subprocess.DEVNULL,
                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                     name=f"{self.name} {self.binary}", owner="backends.external", **kwargs)
        except OSError as exc:
            cell.close()
            raise BackendError(f"could not start {self.name}: {exc}") from exc
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                await _kill_tree(proc)
                raise BackendError(f"{self.name} timed out after {timeout_s:g}s") from exc
            except asyncio.CancelledError:
                await _kill_tree(proc)
                raise
            text_out = out.decode("utf-8", "replace")[:MAX_OUTPUT_CHARS]
            if proc.returncode != 0:
                detail = _ANSI.sub("", err.decode("utf-8", "replace")).strip() or _ANSI.sub("", text_out).strip()
                raise BackendError(f"{self.name} exited with status {proc.returncode}: {detail[:500]}")
            reply = self.parse_output(text_out)
            if not reply:
                raise BackendError(f"{self.name} returned no text")
            return BackendResult(text=reply, raw={"backend": self.name, "exit": proc.returncode, **self.extra_raw(text_out)})
        finally:
            cell.close()          # the turn is over either way: release its job handle

    def extra_raw(self, stdout: str) -> dict:
        """Extra BackendResult.raw fields a product's own output reveals — currently only
        OpenCodeBackend's session id (see its own override); the base default is nothing."""
        return {}


async def _kill_tree(proc) -> None:
    try:
        from bot.agent_runtime import sandbox

        sandbox.kill(proc)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _opencode_events(stdout: str) -> list[dict]:
    """Every line of `opencode run --format json`'s output that parses as a JSON object (its event stream is
    newline-delimited JSON, one event per line - confirmed live: a failing call still exits 0 and prints an
    {"type":"error",...} event rather than going non-zero, so text output alone cannot tell success from failure)."""
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


class OpenCodeBackend(ExternalAgentBackend):
    name = "opencode"

    def build_args(self, prompt, *, cwd, session, timeout_s):
        # --format json: confirmed live to include a "sessionID" on every event (including error events), which
        # --session below needs a real value for on a later call. Without --format json, plain-text mode gives no
        # way at all to learn the session id opencode picked, so a bot instance could never resume its conversation.
        args = ["run", "--format", "json"]
        if self.model:
            args += ["--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        if self.auto_approve:
            args += ["--auto"]
        if cwd:
            args += ["--dir", str(cwd)]
        if session:
            args += ["--session", str(session)]        # confirmed live: `opencode run --help` documents -s/--session as "session id to continue"
        return [*args, "--", prompt]

    def parse_output(self, stdout: str) -> str:
        events = _opencode_events(stdout)
        if not events:
            return super().parse_output(stdout)         # an older opencode without --format json support, or a version that ignored it
        for ev in events:
            if ev.get("type") == "error":
                err = ev.get("error") or {}
                msg = (err.get("data") or {}).get("message") or err.get("name") or "opencode reported an error"
                raise BackendError(f"opencode reported an error: {msg}"[:500])
        # The success event's exact shape was not observed live this session (every real call made hit a
        # provider/credits error before a reply was produced - see the two live-confirmed error cases above), so
        # this reuses the same generic best-effort search OpenClawBackend's known-shape envelope also falls back to,
        # rather than hard-coding an unverified field name.
        return _find_text(events) or super().parse_output(stdout)

    def extra_raw(self, stdout: str) -> dict:
        events = _opencode_events(stdout)
        session_id = next((ev["sessionID"] for ev in reversed(events) if isinstance(ev.get("sessionID"), str) and ev["sessionID"]), None)
        return {"desktop_session_key": session_id} if session_id else {}


class OpenClawBackend(ExternalAgentBackend):
    name = "openclaw"

    def build_args(self, prompt, *, cwd, session, timeout_s):
        args = ["agent", "--message", prompt, "--json", "--timeout", str(int(max(1, timeout_s)))]
        if self.model:
            args += ["--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        else:
            args += ["--session-key", f"abp:{session or 'default'}"]      # exactly one session selector is required
        return args

    def parse_output(self, stdout: str) -> str:
        cleaned = _ANSI.sub("", stdout).strip()
        try:
            data = json.loads(cleaned)
        except ValueError:
            # Tolerate log lines before the envelope: try the last line that parses.
            data = None
            for line in reversed(cleaned.splitlines()):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        if data is None:
            return cleaned
        status = str((data or {}).get("status", "")).lower() if isinstance(data, dict) else ""
        if status in ("error", "failed", "timeout", "cancelled", "canceled"):
            raise BackendError(f"openclaw reported {status}: {(_find_text(data) or '')[:300]}")
        return _find_text(data) or cleaned
