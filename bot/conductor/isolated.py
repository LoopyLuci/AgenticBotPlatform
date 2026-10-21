"""A Factory worker's agent turn and its proof, each in a child process, for a conductor that runs inside ABP itself.

The headless agent (abp_run's ephemeral_environment, abp_agenteval.factory's agent_environment) changes process-global state
while it runs: ABP's database becomes an empty one of its own, every approval is answered by policy (deny), taint is wiped for
every session, and the config overlay and PATH change for everyone. In a CLI process (`abp conductor run`) that is the whole
process and harmless. Inside the live server (a Factory run from Local Chat) it was the whole of ABP: on 2026-10-20 Local
Chat's sessions answered 404 and other sessions' approvals were denied while a worker ran. Here the same calls run in a
child process with the server's environment, so none of that reaches the server.

    turn(...)    factory.drive_agent in a child; returns its RunResult as an object with the same fields
    proof(...)   proof.run in a child, inside the worker's agent environment; returns (passed, output)
    verify(...)  confine.verify (the sandbox probes) in a child

The child is `python -m bot.conductor.isolated turn|proof|verify`, reading one JSON request on stdin and writing one result line.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
MARK = "@@abp-isolated-result@@ "
GRACE_S = 120                       # beyond the work's own time limit: the child's start-up and the model's last answer


class IsolatedError(RuntimeError):
    pass


async def _child(mode: str, request: dict, timeout_s: float) -> dict:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "bot.conductor.isolated", mode, cwd=str(ROOT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(request).encode("utf-8")), timeout_s + GRACE_S)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        if line.startswith(MARK):
            data = json.loads(line[len(MARK):])
            if data.get("error_type"):
                raise IsolatedError(f"{data['error_type']}: {data.get('error', '')}")
            return data
    tail = err.decode("utf-8", "replace").strip().splitlines()[-5:]
    raise IsolatedError(f"the {mode} child exited {proc.returncode} without a result: {' | '.join(tail)[:600]}")


async def turn(prompt: str, wt: Path, ref: str, *, timeout_s: float, max_iterations: int, python: str, shell: str,
               sandbox_paths: Optional[list[str]], extra_tools: Optional[list[str]]) -> SimpleNamespace:
    data = await _child("turn", {"prompt": prompt, "wt": str(wt), "ref": ref, "timeout_s": timeout_s,
                                 "max_iterations": max_iterations, "python": python, "shell": shell,
                                 "sandbox_paths": sandbox_paths, "extra_tools": extra_tools}, timeout_s)
    return SimpleNamespace(**data["result"])


async def proof(command: str, workspace: Path, timeout_s: Optional[int], *, base: str, settings: dict,
                python: str) -> tuple[bool, str]:
    from bot.agent_runtime import run_tests

    limit = float(timeout_s or run_tests.timeout_s())
    data = await _child("proof", {"command": command, "workspace": str(workspace), "timeout_s": timeout_s, "base": base,
                                  "settings": settings, "python": python}, limit)
    if data.get("refused") is not None:
        from bot.conductor import proof as proof_mod

        raise proof_mod.ProofRefused(data["refused"])
    return bool(data["passed"]), str(data["output"])


async def verify(workspace: Path, extra_paths: Optional[list[str]]) -> None:
    """confine.verify (the sandbox probes) in a child; raises confine.SandboxUnavailable as it would."""
    data = await _child("verify", {"workspace": str(workspace), "extra_paths": extra_paths}, 300)
    if data.get("unavailable") is not None:
        from bot.conductor import confine

        raise confine.SandboxUnavailable(data["unavailable"])


# ------------------------------------------------------------------ the child
def _run_verify(req: dict) -> dict:
    from bot.conductor import confine

    try:
        confine.verify(Path(req["workspace"]), extra_paths=req["extra_paths"])
    except confine.SandboxUnavailable as exc:
        return {"unavailable": str(exc)}
    return {"ok": True}


def _run_turn(req: dict) -> dict:
    from abp_agenteval import factory
    from bot.conductor import llm

    transport, name = llm.transport_for_ref(req["ref"])
    res = factory.drive_agent(req["prompt"], Path(req["wt"]), transport, name, timeout_s=req["timeout_s"],
                              max_iterations=req["max_iterations"], python=req["python"], shell=req["shell"],
                              sandbox_paths=req["sandbox_paths"], extra_tools=req["extra_tools"])
    return {"result": res.to_dict() if hasattr(res, "to_dict") else {"ok": True, "reply": str(res)}}


def _run_proof(req: dict) -> dict:
    from abp_agenteval import factory
    from bot.conductor import proof as proof_mod

    try:
        passed, output = asyncio.run(proof_mod.run(req["command"], Path(req["workspace"]), req["timeout_s"], base=req["base"],
                                                   env=lambda: factory.agent_environment(req["settings"], python=req["python"])))
    except proof_mod.ProofRefused as exc:
        return {"refused": str(exc)}
    return {"passed": passed, "output": output}


def main(argv: list[str]) -> int:
    from bot.sandbox_ns import guard

    guard.install()                  # no console window may appear, whatever the child starts
    mode = argv[0] if argv else ""
    try:
        req = json.loads(sys.stdin.read() or "{}")
        data = {"turn": _run_turn, "proof": _run_proof, "verify": _run_verify}[mode](req)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which raises it there
        data = {"error_type": type(exc).__name__, "error": str(exc)[:2000]}
    sys.stdout.write("\n" + MARK + json.dumps(data, default=str) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
