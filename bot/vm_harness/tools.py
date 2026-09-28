"""The agent's VM-Harness tools. Offered while VM-Harness is installed. Reading is free; anything that changes a VM, a
container or the window asks first.

    vmh_status       installed? up to date? hub running? which hypervisors and container engines work here
    vmh_vms          every VM on every hypervisor, with its state
    vmh_operations   search VM-Harness's operations (160+), or one's arguments
    vmh_read         run any operation that changes nothing (status, metrics, logs, screenshots, lists...)
    vmh_call         run any operation (asks first)
    vmh_gui_look     the VM-Harness window: its panels, the widgets on one, a widget's contents, a screenshot
    vmh_gui_act      drive the window: open it, switch panels, click, fill in, select, type, answer dialogs (asks first)
    vmh_setup        install / update VM-Harness from its repo, start or stop its hub (asks first)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.vm_harness import client, harness
from bot.vm_harness.client import HarnessError

MAX_OUT = 12_000
GUI_LOOK = {"state", "panels", "inspect", "find", "read", "screenshot", "methods", "messages", "status"}
GUI_ACT = {"launch", "open", "window", "click", "set", "select", "type", "key", "invoke", "dialog", "wait"}


def _out(value: Any) -> str:
    if isinstance(value, dict) and value.get("format") == "png" and "base64" in value:
        value = {**{k: v for k, v in value.items() if k != "base64"},
                 "note": "PNG captured; the dashboard's VM-Harness page shows it"}
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except HarnessError as exc:
        return f"Error ({exc.code}): {exc}"


_enabled_cache: dict[str, float | bool] = {"at": 0.0, "on": False}


def _enabled() -> bool:
    """Whether VM-Harness is installed here (or a remote hub is configured). Checked every turn, so it only looks
    for files and is cached for 30 seconds: no git, no network."""
    import time
    if time.monotonic() - float(_enabled_cache["at"]) < 30:
        return bool(_enabled_cache["on"])
    try:
        on = harness._is_checkout(harness.install_dir()) or bool(client._cfg().get("url"))
    except Exception:  # noqa: BLE001
        on = False
    _enabled_cache.update(at=time.monotonic(), on=on)
    return on


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def status(inp, **_):
        return await _run(harness.summary)

    async def vms(inp, **_):
        return await _run(lambda: [{k: v.get(k) for k in ("name", "backend", "state", "error") if v.get(k)}
                                   for v in client.call("vm.list", {"backend": inp.get("backend") or ""})])

    async def operations(inp, **_):
        def run():
            if inp.get("operation"):
                return client.operation(str(inp["operation"]))
            q = str(inp.get("query") or "").lower().split()
            group = str(inp.get("group") or "")
            ops = client.operations()
            if not q and not group:
                counts: dict[str, int] = {}
                for o in ops:
                    counts[o["group"]] = counts.get(o["group"], 0) + 1
                return {"groups": counts, "hint": "pass query or group to list operations, operation for one's arguments"}
            return [{"id": o["id"], "summary": o["summary"], "changes": o["mutating"], "destructive": o["destructive"]}
                    for o in ops if (not group or o["group"] == group)
                    and all(w in f"{o['id']} {o['summary']}".lower() for w in q)]
        return await _run(run)

    async def read(inp, **_):
        op_id = str(inp.get("operation") or "")

        def run():
            op = client.operation(op_id)
            if op["mutating"]:
                raise HarnessError(f"{op_id} changes something; use vmh_call for it", code="mutating")
            return client.call(op_id, dict(inp.get("args") or {}))
        return await _run(run)

    async def call(inp, **_):
        op_id = str(inp.get("operation") or "")
        if not op_id:
            return "Error: give the operation id (see vmh_operations)"
        return await _run(client.call, op_id, dict(inp.get("args") or {}), timeout=float(inp.get("timeout_s") or 900))

    def gui(allowed: set[str]):
        async def handler(inp, **_):
            action = str(inp.get("action") or "")
            if action not in allowed:
                return f"Error: action is one of {', '.join(sorted(allowed))}"
            args = {k: v for k, v in inp.items() if k != "action" and v is not None}
            if action == "launch":
                return await _run(harness.open_window, float(args.get("wait_s") or 60))
            return await _run(client.call, f"gui.{action}", args, timeout=120)
        return handler

    async def setup(inp, **_):
        action = str(inp.get("action") or "")
        fns = {"install": harness.setup, "update": harness.update, "start": harness.start_hub, "stop": harness.stop_hub,
               "jobs": harness.jobs, "register_mcp": harness.register_mcp}
        if action not in fns:
            return f"Error: action is one of {', '.join(fns)}"
        return await _run(fns[action])

    target = {"type": "object", "description": "{panel?, id} or {panel?, text}: a widget, by the id vmh_gui_look "
                                               "inspect gave it or by its text / label",
              "properties": {"panel": {"type": "string"}, "id": {"type": "string"}, "text": {"type": "string"}}}

    reg("vmh_status", "VM-Harness at a glance: installed where, which commit, updates waiting, whether its hub is running and "
        "its window attached, and which hypervisors (QEMU, VirtualBox, VMware, Hyper-V, WSL, KVM) and container engines "
        "(Docker, Podman, Kubernetes) work on this machine.", {}, [], status, permission="read", read_only=True)
    reg("vmh_vms", "Every virtual machine on every hypervisor VM-Harness can reach, with its state (running, stopped, "
        "suspended...). backend limits it to one hypervisor.", {"backend": {"type": "string"}}, [], vms,
        permission="read", read_only=True)
    reg("vmh_operations", "Search VM-Harness's operations: VMs (vm.*: start, stop, snapshots, exec in the guest, disks, "
        "NICs, clone, export...), qemu.* (raw QMP, qemu-img), containers/images/networks/volumes, compose.*, k8s.*, iso.*, "
        "host.*, audit.*, gui.*. With operation: that operation's arguments (JSON Schema).",
        {"query": {"type": "string"}, "group": {"type": "string"}, "operation": {"type": "string"}}, [], operations,
        permission="read", read_only=True)
    reg("vmh_read", "Run a VM-Harness operation that changes nothing (vm.status, vm.metrics, vm.screenshot, "
        "container.logs, k8s.list_pods, qemu.img_info, audit.query...). Refuses operations that change something.",
        {"operation": {"type": "string"}, "args": {"type": "object"}}, ["operation"], read,
        permission="read", read_only=True)
    reg("vmh_call", "Run any VM-Harness operation by id with its arguments: start, stop or create VMs, snapshots, run "
        "commands in guests, containers, Kubernetes, ISO downloads... (find ids and arguments with vmh_operations).",
        {"operation": {"type": "string"}, "args": {"type": "object"}, "timeout_s": {"type": "number"}}, ["operation"],
        call, permission="external", read_only=False)
    reg("vmh_gui_look", "Look at the VM-Harness window without changing it. action: status (is it open), state, panels "
        "(its 34 pages), inspect (the widgets on a panel: ids, labels, values, table rows), find (widgets matching query), "
        "read (one widget's full contents), screenshot, methods (a panel's callable methods), messages (open dialogs).",
        {"action": {"type": "string", "enum": sorted(GUI_LOOK)}, "panel": {"type": "string"}, "query": {"type": "string"},
         "target": target, "max_rows": {"type": "integer"}, "max_width": {"type": "integer"}}, ["action"],
        gui(GUI_LOOK), permission="read", read_only=True)
    reg("vmh_gui_act", "Drive the VM-Harness window as a person would. action: launch (open it), open (a panel), window "
        "(show/hide/raise/minimize/maximize/restore/resize), click, set (a field's value), select (an item or tab), type, "
        "key (a shortcut), invoke (a panel method with args/kwargs), dialog (click a button in an open dialog), wait (until "
        "a widget shows text or is enabled).",
        {"action": {"type": "string", "enum": sorted(GUI_ACT)}, "panel": {"type": "string"}, "target": target,
         "value": {}, "item": {}, "text": {"type": "string"}, "keys": {"type": "string"}, "enter": {"type": "boolean"},
         "method": {"type": "string"}, "args": {"type": "array"}, "kwargs": {"type": "object"},
         "button": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"},
         "enabled": {"type": "boolean"}, "timeout_s": {"type": "number"}, "wait_s": {"type": "number"}},
        ["action"], gui(GUI_ACT), permission="external", read_only=False)
    reg("vmh_setup", "Install or maintain VM-Harness. action: install (clone it from its repo if missing, make its venv, "
        "install dependencies), update (pull the latest from its repo and reinstall; refuses if there is uncommitted work), "
        "start / stop (its hub), jobs (progress of install/update), register_mcp (add its MCP server to ABP's external "
        "MCP servers).", {"action": {"type": "string", "enum": ["install", "update", "start", "stop", "jobs",
                                                                  "register_mcp"]}},
        ["action"], setup, permission="config", read_only=False)


register_tools()
