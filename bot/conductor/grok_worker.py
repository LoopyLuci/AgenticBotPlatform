"""Grok Build as a Factory worker, in one of two ways (`conductor.grok.mode`):

- **advisor**: Grok plans, a local model does the work. Grok gets the task and the text of its files, with no tools at all
  (`--tools ""`, no web search, plan permission mode, run in an empty folder), and answers with a plan. The confined local
  worker (AgentLoopWorker: AppContainer, no network) then makes the change with that plan in its prompt. Grok never touches
  the worktree and runs nothing, so this needs no network approval for a leaf.
- **confined**: Grok does the work itself, inside an AppContainer of its own (appcontainer.AGENT_PROFILE_NAME, never the
  offline container the local workers share): read-write on the task's worktree only, read on its own `~/.grok`, write on its
  session/log folders and its sign-in file, and outbound internet (the `internetClient` capability). Loopback and the local
  network stay blocked, so ABP's server and the LAN are out of reach; the rest of the home folder is not readable. A leaf with
  network is the owner's call, so this runs only when `conductor.grok.network_approved` is true.
- **auto**: confined when it is approved and available, else advisor - so a run never stops for want of the approval.

Neither mode ever passes `--always-approve` or `bypassPermissions`. The reviewer, proof and integrator treat the result exactly
like any other worker's branch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bot.conductor.grok")

MODES = ("off", "advisor", "confined", "auto")
_confined_broken = ""         # why confined Grok could not act in this process; auto then uses advisor until a restart
FILE_CHARS = 20_000           # per file shown to the advisor
TOTAL_CHARS = 80_000          # all files together


class GrokUnavailable(RuntimeError):
    release_task = True       # not the task's fault: the board task is handed back


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def grok_exe() -> Optional[Path]:
    """The native binary (the npm `grok` is a node launcher, which cannot start inside an AppContainer)."""
    for p in (grok_home() / "bin" / ("grok.exe" if os.name == "nt" else "grok"),):
        if p.is_file():
            return p
    return None


def installed() -> bool:
    """The Grok CLI is here at all (the native binary or the npm launcher the advisor uses)."""
    import shutil

    return grok_exe() is not None or shutil.which("grok") is not None


def settings(cfg: Optional[dict] = None) -> dict:
    from bot import conductor

    return dict((cfg or conductor.settings()).get("grok") or {})


def resolve_mode(gcfg: dict) -> str:
    """'advisor' or 'confined' for this run; raises GrokUnavailable when the asked-for mode cannot run."""
    mode = str(gcfg.get("mode") or "off")
    if mode not in MODES or mode == "off":
        raise GrokUnavailable(f"conductor.grok.mode is {mode!r}")
    if mode == "advisor":
        return "advisor"
    from bot.agent_runtime import appcontainer

    why = ("" if gcfg.get("network_approved") else "the owner has not approved network for a Grok leaf (conductor.grok.network_approved)") \
        or ("" if appcontainer.is_supported() else "no Windows AppContainer here") \
        or ("" if grok_exe() else f"no native grok binary under {grok_home() / 'bin'}")
    if not why and mode == "auto" and _confined_broken:
        why = f"confined Grok could not act earlier in this run of ABP ({_confined_broken})"
    if not why:
        return "confined"
    if mode == "confined":
        raise GrokUnavailable(f"confined Grok cannot run: {why}")
    logger.info("grok worker: advisor mode (%s)", why)
    return "advisor"


def _files_block(wt: Path, files: list[str]) -> str:
    out, total = [], 0
    for rel in files:
        p = (wt / rel)
        try:
            if not p.resolve().is_relative_to(wt.resolve()) or not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")[:FILE_CHARS]
        except OSError:
            continue
        if total + len(text) > TOTAL_CHARS:
            break
        total += len(text)
        out.append(f"--- {rel}\n{text}")
    return "\n".join(out)


def advice_prompt(spec: dict, files_text: str, feedback: str) -> str:
    return (f"You are the planner for a coding task another agent will carry out. You cannot run tools; answer in text only.\n\n"
            f"Task: {spec['title']}\n\n{spec.get('detail') or ''}\n\nOverall goal: {spec.get('goal', '')}\n"
            f"Proof that it is done: {(spec.get('proof') or {}).get('command', '') or '(the tests)'}\n"
            + (f"\nThe last attempt was rejected for:\n{feedback}\n" if feedback else "")
            + (f"\nCurrent files:\n{files_text}\n" if files_text else "")
            + "\nGive a short, exact plan: which files to change, the functions and edits, and the tests to add. Never remove "
              "or rename an existing feature. Code snippets are welcome; keep it under 60 lines.")


async def _advise_default(prompt: str, *, model: str, timeout_s: float) -> str:
    from bot.backends.external_agent_backend import GrokBackend

    with tempfile.TemporaryDirectory(prefix="abp-grok-advice-") as empty:
        backend = GrokBackend("grok", model=model or None, tools="", web_search=False, permission_mode="plan", cwd=empty)
        res = await backend.ask(prompt, context={"cwd": empty}, timeout_s=timeout_s)
    return res.text


def confined_argv(wt: Path, prompt_file: Path, *, model: str, python: str = sys.executable) -> list[str]:
    """The AppContainer launcher's argv for one confined Grok turn in worktree `wt`."""
    from bot.agent_runtime import appcontainer

    home, exe = grok_home(), grok_exe()
    if exe is None:
        raise GrokUnavailable(f"no native grok binary under {home / 'bin'}")
    for sub in ("sessions", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    # auto, not acceptEdits: measured live (grok 1.0.62), a headless acceptEdits turn cancels at its first edit. Never
    # --always-approve: the AppContainer is what bounds it (this worktree, ~/.grok and internet only). Known open issue
    # (2026-10-24): inside the container an edit to an EXISTING file is still cancelled (a new file is written), while the same
    # flags outside it edit fine; git cannot start in the container (it cannot open its null device), the leading suspect.
    # Auto mode falls back to advisor when that happens (GrokWorker.__call__).
    cmd = (f'"{exe}" --prompt-file "{prompt_file}" --output-format json --permission-mode auto '
           f'--disable-web-search --no-auto-update --cwd "{wt}"' + (f' --model {model}' if model else ""))
    return appcontainer.launcher_argv(python, cmd, wt, wt, extra_paths=[str(home)],
                                      write_paths=[str(home / "sessions"), str(home / "logs")],
                                      write_files=[str(home / "auth.json")], internet=True,
                                      profile=appcontainer.AGENT_PROFILE_NAME)


class GrokWorker:
    """A conductor Worker: `await worker(spec, feedback) -> dict`, with `local` (an AgentLoopWorker) doing the advisor-mode work
    and providing the worktree and commit either way."""

    def __init__(self, local: Any, *, cfg: Optional[dict] = None,
                 advise: Optional[Callable[..., Awaitable[str]]] = None,
                 run_confined: Optional[Callable[[list[str], float], Awaitable[tuple[int, str]]]] = None):
        self.local = local
        self.gcfg = settings(cfg)
        self.model = str(self.gcfg.get("model") or "")
        self.timeout_s = float(self.gcfg.get("timeout_s") or 900)
        self.advise = advise or _advise_default
        self.run_confined = run_confined or _run_argv
        self.mode = resolve_mode(self.gcfg)              # decided once, up front: a run does not switch modes halfway

    async def __call__(self, spec: dict, feedback: str) -> dict:
        if self.mode == "advisor":
            wt, _ = await asyncio.to_thread(self.local.prepare, spec)
            files = list(dict.fromkeys(spec.get("files") or []))
            text = await asyncio.to_thread(_files_block, wt, files)
            try:
                plan = await self.advise(advice_prompt(spec, text, feedback), model=self.model, timeout_s=min(self.timeout_s, 300))
            except Exception as exc:  # noqa: BLE001 - the local worker still does the task without the plan
                logger.info("grok advisor failed (%s); the local worker goes on without its plan", exc)
                plan = ""
            out = await self.local({**spec, "advice": plan}, feedback)
            return {**out, "grok": {"mode": "advisor", "advice_chars": len(plan)}}
        try:
            return await self._confined(spec, feedback)
        except Exception as exc:
            from bot.conductor import llm

            if str(self.gcfg.get("mode")) != "auto" or not isinstance(exc, llm.WorkerModelError) or "cancelled" not in str(exc):
                raise
            global _confined_broken
            _confined_broken = "a confined turn stopped before acting"
            logger.warning("grok worker: confined Grok stopped before acting; auto falls back to advisor (%s)", exc)
            self.mode = "advisor"                        # this task and the rest of the run: the local worker does the work
            return await self(spec, feedback)

    async def _confined(self, spec: dict, feedback: str) -> dict:
        from bot.conductor import integrator

        wt, start = await asyncio.to_thread(self.local.prepare, spec)
        prompt = (f"Task: {spec['title']}\n\n{spec.get('detail') or ''}\n\nOverall goal: {spec.get('goal', '')}\n"
                  f"Files likely involved: {', '.join(spec.get('files') or []) or '(find them)'}\n"
                  f"It is done when this passes: {(spec.get('proof') or {}).get('command', '') or '(the tests)'}\n"
                  "Rules: never remove or rename an existing feature; do not delete files; add or update a test for what you "
                  "change; keep the change small. Do not commit; just leave the changes in the working tree."
                  + (f"\n\nYour previous attempt was REJECTED by the reviewer. Fix exactly these:\n{feedback}" if feedback else ""))
        pfile = wt / ".abp-grok-prompt.txt"             # inside the worktree: the only folder the container can read besides ~/.grok
        try:
            pfile.write_text(prompt, encoding="utf-8")
            code, out = await self.run_confined(confined_argv(wt, pfile, model=self.model), self.timeout_s)
        finally:
            pfile.unlink(missing_ok=True)              # never committed with the task
        reply = _reply(out)
        if code != 0 or _cancelled(out):
            from bot.conductor import llm

            why = f"exited {code}" if code != 0 else "stopped before acting (cancelled)"
            raise llm.WorkerModelError(f"confined grok {why}: {reply[:300]}")
        await asyncio.to_thread(self.local._commit, wt, spec)
        made = integrator._git(wt, "rev-list", "--count", f"{start}..HEAD", check=False).stdout.strip()
        return {"branch": spec["branch"], "worktree": str(wt), "start": start, "shell": "grok-confined",
                "empty": made in ("", "0"), "summary": reply[:500], "grok": {"mode": "confined"}}


def _events(out: str) -> list[dict]:
    """Grok's JSON from the launcher's output, which is stdout and stderr together: the pretty-printed document is cut out of
    whatever surrounds it when the whole text does not parse."""
    from bot.backends.external_agent_backend import _grok_events

    events = _grok_events(out)
    if not events and "{" in out:
        events = _grok_events(out[out.find("{"):out.rfind("}") + 1])
    return events


def _cancelled(out: str) -> bool:
    return any(ev.get("stopReason") == "cancelled" for ev in _events(out))


def _reply(out: str) -> str:
    from bot.backends.external_agent_backend import _find_text, _grok_error

    events = _events(out)
    return _grok_error(json.dumps(events[-1]) if events else out) or _find_text(events) or out.strip()


async def _run_argv(argv: list[str], timeout_s: float) -> tuple[int, str]:
    def go() -> tuple[int, str]:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return 124, json.dumps({"type": "error", "message": f"timed out after {timeout_s:g}s"})
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    return await asyncio.to_thread(go)
