"""run_script: run one of the repository's own generator scripts, for agents that have no general shell.

Some changes cannot be made by editing files: a generated file (the OpenAPI document, the TypeScript SDK, the capability
index) must come from its generator. This tool runs exactly `python <script>` for a script on an allowlist and nothing else:

* `script` must be one of `native_agent.run_script.allow` (default: scripts/export_openapi.py, scripts/gen_capabilities.py),
  given repository-relative; no arguments, no options, no shell syntax;
* it runs only inside a confining sandbox with no network: the backend must be windows_job, docker, wsl or ssh and
  `sandbox.network` must be "none". On the `local` backend, or with network allowed, it refuses (AGENTS.md: leaf workers are
  OS-confined before they touch a real task). It starts through the same sandbox as run_shell and run_tests;
* a timeout (`native_agent.run_script.timeout_s`, default 300 s) stops the whole process tree.

The answer is the exit code and the output's tail. Approval is the normal one for a tool that runs code.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from bot.agent_runtime import shell, toolspec
from bot.agent_runtime.errors import ToolError, safe_path

DEFAULT_ALLOW = ("scripts/export_openapi.py", "scripts/gen_capabilities.py")
CONFINING = ("windows_job", "docker", "wsl", "ssh")
DEFAULT_TIMEOUT_S = 300
MAX_TAIL = 4000


def _config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("run_script")) or {}
    except Exception:  # noqa: BLE001
        return {}


def allowed() -> list[str]:
    allow = _config().get("allow")
    return [str(a).replace("\\", "/") for a in allow] if isinstance(allow, list) else list(DEFAULT_ALLOW)


def timeout_s() -> int:
    try:
        return max(1, min(int(_config().get("timeout_s", DEFAULT_TIMEOUT_S)), 3600))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def check_confined() -> None:
    """Refuse unless a confining sandbox with no network is configured."""
    from bot.agent_runtime import sandbox

    name = sandbox.backend()
    if name not in CONFINING:
        raise ToolError(f"run_script needs a confining sandbox ({', '.join(CONFINING)}); this agent's sandbox is {name!r}, "
                        "so the script is not run")
    if sandbox.network() != "none":
        raise ToolError("run_script needs sandbox.network 'none'; the script is not run with network access")


def clean_script(workspace: Path, script) -> str:
    if not isinstance(script, str) or not script.strip():
        raise ToolError('script is required, e.g. {"script": "scripts/export_openapi.py"}. Allowed: ' + ", ".join(allowed()))
    rel = script.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel not in allowed():
        raise ToolError(f"{script!r} is not an allowed script. Allowed: {', '.join(allowed())} (no arguments)")
    if not safe_path(Path(workspace), rel).is_file():
        raise ToolError(f"{rel!r} does not exist in this checkout")
    return rel


def build_command(rel: str) -> str:
    from bot.agent_runtime import run_tests

    py = run_tests._pytest_python()            # the interpreter the sandbox can reach (the same choice run_tests makes)
    if os.name == "nt":
        return f'"{py}" ' + subprocess.list2cmdline([rel])
    return shlex.quote(py) + " " + shlex.quote(rel)


async def _run_script(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    workspace = Path(workspace).resolve()
    rel = clean_script(workspace, inp.get("script") or inp.get("path"))
    check_confined()
    output = await shell._run_fresh(build_command(rel), workspace, workspace, timeout_s())
    tail = output.strip()[-MAX_TAIL:]
    return f"ran {rel}\n{tail}" if tail else f"ran {rel} (no output)"


def register_all() -> None:
    toolspec.register(
        {"name": "run_script",
         "description": "Run one of the repository's generator scripts (for example scripts/export_openapi.py, which regenerates "
                        "docs/api/openapi.json and the TypeScript SDK) in the sandbox without network, and get its output. "
                        "Only the allowed scripts, no arguments. Use it instead of editing generated files by hand.",
         "input_schema": {"type": "object",
                          "properties": {"script": {"type": "string", "description": "The script, relative to the working directory."}},
                          "required": ["script"]}},
        toolspec.ToolSpec("run_script", "execute", read_only=False, max_output_chars=6_000, origin="registered"),
        _run_script)


register_all()
