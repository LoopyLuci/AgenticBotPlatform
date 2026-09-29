"""The agent's module tools: one set for every module (see registry.BUILTIN), each taking `module` (its id or name).
Reading is free; building, updating, starting and running operations that change something ask first. Every tool
takes `machine`: a linked server's name, to do the same on that machine through its ABP (its owner must allow
peers.remote_control: [modules]).

    module_list        every module: what it is, installed? built? hub running? updates waiting?
    module_status      one module in full: its checkout, commit and updates, hub, toolchain, host, jobs
    module_operations  search what a module's hub can do, or one operation's arguments
    module_read        run an operation that changes nothing
    module_call        run any operation (asks first)
    module_setup       install, update, build, run its pipeline, start or stop its hub, open its window or terminal
                       UI, add its MCP server, follow jobs (asks first)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from bot.modules import harness, registry
from bot.modules.client import ModuleError

MAX_OUT = 12_000
API = "/api/modules"
MACHINE = {"type": "string", "description": "a linked server's name (Peers page) to do this on that machine instead of "
                                            "this one; its owner must allow it (peers.remote_control: [modules])"}
MODULE = {"type": "string", "description": "the module's id or name: " + ", ".join(d["module"]["id"] for d in registry.BUILTIN)}
SETUP_ACTIONS = {"install": ("POST", "setup"), "update": ("POST", "update"), "build": ("POST", "build"),
                 "pipeline": ("POST", "pipeline"), "start": ("POST", "hub/start"), "stop": ("POST", "hub/stop"),
                 "open_gui": ("POST", "gui"), "open_tui": ("POST", "tui"), "register_mcp": ("POST", "mcp"),
                 "jobs": ("GET", "jobs")}


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _mid(inp: dict) -> str:
    ref = str(inp.get("module") or "").strip()
    if not ref:
        raise ModuleError("give module (see module_list)", code="bad_request")
    if inp.get("machine"):
        return ref.lower()       # the other machine resolves it
    m = registry.find(ref)
    if m is None:
        raise ModuleError(f"no module {ref!r}; known: {', '.join(sorted(registry.modules()))}", code="not_found")
    return m.id


async def _local(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except ModuleError as exc:
        return f"Error ({exc.code}): {exc}"


async def _remote(machine: str, method: str, path: str, body: Any = None, *, shape=None) -> str:
    from bot import peers
    try:
        data = await peers.on_machine(machine, method, API + path, body)
        return _out(shape(data) if shape else data)
    except (peers.PeerError, ModuleError) as exc:
        return f"Error: {exc}"


def _filter_ops(ops: list, inp: dict) -> Any:
    words = str(inp.get("query") or "").lower().split()
    group = str(inp.get("group") or "")
    if not words and not group:
        counts: dict[str, int] = {}
        for o in ops:
            counts[o["group"]] = counts.get(o["group"], 0) + 1
        return {"operations": len(ops), "groups": counts,
                "hint": "pass query or group to list operations, or operation for one's arguments"}
    return [{"id": o["id"], "summary": o["summary"], "changes": o["mutating"], "destructive": o["destructive"]}
            for o in ops if (not group or o["group"] == group) and all(w in f"{o['id']} {o['summary']}".lower() for w in words)]


def _find_op(ops: list, op_id: str) -> dict:
    for o in ops:
        if o["id"] == op_id:
            return o
    raise ModuleError(f"no operation {op_id!r}", code="not_found")


_enabled_cache: dict[str, Any] = {"at": 0.0, "on": False}


def _enabled() -> bool:
    """Offered once any module is installed here, or a server is linked. Checked every turn: cached 30 s."""
    if time.monotonic() - float(_enabled_cache["at"]) < 30:
        return bool(_enabled_cache["on"])
    try:
        on = any(registry.is_checkout(m, harness.checkout_dir(m)) for m in registry.modules().values())
        if not on:
            from bot import peers
            on = peers.any_linked()
    except Exception:  # noqa: BLE001
        on = False
    _enabled_cache.update(at=time.monotonic(), on=on)
    return on


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        props = {**props, "machine": MACHINE}
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def list_(inp, **_):
        if inp.get("machine"):
            return await _remote(inp["machine"], "GET", "")
        return await _local(lambda: {"modules": harness.overview(), "manifest_errors": registry.manifest_errors()})

    async def status(inp, **_):
        try:
            mid = _mid(inp)
        except ModuleError as exc:
            return f"Error: {exc}"
        fetch = bool(inp.get("fetch"))
        if inp.get("machine"):
            return await _remote(inp["machine"], "GET", f"/{mid}?fetch={'1' if fetch else '0'}")
        return await _local(harness.status, mid, fetch=fetch)

    async def operations(inp, **_):
        try:
            mid = _mid(inp)
        except ModuleError as exc:
            return f"Error: {exc}"
        op = str(inp.get("operation") or "")
        shape = (lambda ops: _find_op(ops, op)) if op else (lambda ops: _filter_ops(ops, inp))
        if inp.get("machine"):
            return await _remote(inp["machine"], "GET", f"/{mid}/operations", shape=shape)
        return await _local(lambda: shape(harness.operations(mid)))

    async def read(inp, **_):
        try:
            mid = _mid(inp)
        except ModuleError as exc:
            return f"Error: {exc}"
        op_id = str(inp.get("operation") or "")
        args = dict(inp.get("args") or {})
        if inp.get("machine"):
            from bot import peers
            try:
                op = _find_op(await peers.on_machine(inp["machine"], "GET", f"{API}/{mid}/operations"), op_id)
            except (peers.PeerError, ModuleError) as exc:
                return f"Error: {exc}"
            if op["mutating"]:
                return f"Error: {op_id} changes something; use module_call for it"
            return await _remote(inp["machine"], "POST", f"/{mid}/call", {"operation": op_id, "args": args},
                                 shape=lambda d: d.get("result") if isinstance(d, dict) else d)

        def go():
            if harness.operation(mid, op_id)["mutating"]:
                raise ModuleError(f"{op_id} changes something; use module_call for it", code="mutating")
            return harness.call(mid, op_id, args)
        return await _local(go)

    async def call(inp, **_):
        try:
            mid = _mid(inp)
        except ModuleError as exc:
            return f"Error: {exc}"
        op_id = str(inp.get("operation") or "")
        if not op_id:
            return "Error: give the operation id (see module_operations)"
        args = dict(inp.get("args") or {})
        if inp.get("machine"):
            return await _remote(inp["machine"], "POST", f"/{mid}/call", {"operation": op_id, "args": args},
                                 shape=lambda d: d.get("result") if isinstance(d, dict) else d)
        return await _local(harness.call, mid, op_id, args)

    async def setup(inp, **_):
        try:
            mid = _mid(inp)
        except ModuleError as exc:
            return f"Error: {exc}"
        action = str(inp.get("action") or "")
        if action == "job":
            job_id = str(inp.get("job_id") or "")
            if inp.get("machine"):
                return await _remote(inp["machine"], "GET", f"/jobs/{job_id}")
            return await _local(harness.job, job_id)
        if action not in SETUP_ACTIONS:
            return f"Error: action is one of {', '.join([*SETUP_ACTIONS, 'job'])}"
        method, route = SETUP_ACTIONS[action]
        if inp.get("machine"):
            return await _remote(inp["machine"], method, f"/{mid}/{route}")
        fns = {"install": harness.setup, "update": harness.update, "build": harness.build, "pipeline": harness.run_pipeline,
               "start": harness.start_hub, "stop": harness.stop_hub, "open_gui": harness.open_gui,
               "open_tui": harness.open_tui, "register_mcp": harness.register_mcp, "jobs": harness.jobs}
        return await _local(fns[action], mid)

    reg("module_list", "Every module ABP knows: separate programs it installs, updates, builds and drives (VM-Harness, "
        "Hermes-Manager, TransferDaemon, ModelMistress, Continuum, TridentDroid, BrainBuilder, Wrightspace, and any "
        "added in config). For each: what it is, installed? built? hub running? updates waiting? a job running?",
        {}, [], list_, permission="read", read_only=True)
    reg("module_status", "One module in full: its checkout (path, commit, branch, uncommitted files, commits behind "
        "and ahead; fetch=true checks the remote first), whether it is built, its hub, the tools it needs and whether "
        "they are installed, whether this machine can run it, and running jobs.",
        {"module": MODULE, "fetch": {"type": "boolean"}}, ["module"], status, permission="read", read_only=True)
    reg("module_operations", "Search what a module's hub can do: with no query, its groups; with query or group, the "
        "matching operations; with operation, that one's arguments (JSON Schema).",
        {"module": MODULE, "query": {"type": "string"}, "group": {"type": "string"}, "operation": {"type": "string"}},
        ["module"], operations, permission="read", read_only=True)
    reg("module_read", "Run a module operation that changes nothing (see module_operations). Refuses operations that "
        "change something.", {"module": MODULE, "operation": {"type": "string"}, "args": {"type": "object"}},
        ["module", "operation"], read, permission="read", read_only=True)
    reg("module_call", "Run any operation of a module's hub by id with its arguments (see module_operations).",
        {"module": MODULE, "operation": {"type": "string"}, "args": {"type": "object"}}, ["module", "operation"], call,
        permission="external", read_only=False)
    reg("module_setup", "Install or maintain a module. action: install (clone it from its repo if it is not here, then "
        "build it), update (pull with fast-forward only and rebuild; refuses over uncommitted work), build, pipeline "
        "(run its own local CI/CD pipeline), start / stop (its hub), open_gui (its window), open_tui (its terminal UI "
        "in a console), register_mcp (add its MCP server to ABP's), jobs (its recent jobs), job (one job's log: "
        "job_id). Installs, updates, builds and pipelines run in the background: follow them with jobs or job.",
        {"module": MODULE, "action": {"type": "string", "enum": [*SETUP_ACTIONS, "job"]}, "job_id": {"type": "string"}},
        ["module", "action"], setup, permission="config", read_only=False)


register_tools()
