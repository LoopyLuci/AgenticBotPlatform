"""The agent's Hermes Manager tools. Offered while Hermes Manager is installed. Reading is free; anything that changes
Hermes (the gateway, config, backups, updates, skills, cron...) or the window asks first.

    hm_status       installed? built? bridge running? window open? Hermes's health
    hm_operations   search the operations (gateway, logs, sessions, chat, config, updates, backups, tools, gui)
    hm_read         run an operation that changes nothing (gateway status, log tails, sessions, config, skills...)
    hm_call         run any operation (asks first)
    hm_gui_look     the Hermes Manager window: sections, elements on screen, text, a screenshot
    hm_gui_act      drive the window: open it, switch sections, click, fill in, select, tick, keys (asks first)
    hm_setup        install / update Hermes Manager from its repo, start or stop a headless bridge (asks first)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from bot.hermes_manager import client, harness
from bot.hermes_manager.client import ManagerError

MAX_OUT = 12_000
GUI_LOOK = {"state", "sections", "inspect", "find", "read", "text", "screenshot", "wait"}
GUI_ACT = {"launch", "open", "click", "fill", "select", "check", "key", "window"}


def _out(value: Any) -> str:
    if isinstance(value, dict) and value.get("format") == "png" and "base64" in value:
        value = {**{k: v for k, v in value.items() if k != "base64"}, "note": "PNG captured; the Hermes Manager page shows it"}
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except ManagerError as exc:
        return f"Error: {exc}"


_on: dict[str, float | bool] = {"at": 0.0, "on": False}


def _enabled() -> bool:
    """Installed here? Checked every turn, so only files are looked at, cached for 30 seconds."""
    if time.monotonic() - float(_on["at"]) < 30:
        return bool(_on["on"])
    try:
        on = harness._is_checkout(harness.install_dir())
    except Exception:  # noqa: BLE001
        on = False
    _on.update(at=time.monotonic(), on=on)
    return on


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register({"name": name, "description": description,
                           "input_schema": {"type": "object", "properties": props, "required": required}},
                          toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only,
                                            origin="registered"), handler, enabled=_enabled)

    async def status(inp, **_):
        return await _run(harness.status)

    async def operations(inp, **_):
        def go():
            if inp.get("operation"):
                return client.operation(str(inp["operation"]))
            q = str(inp.get("query") or "").lower().split()
            ops = client.operations()
            return [{"id": o["id"], "summary": o["summary"], "changes": o["mutating"]} for o in ops
                    if (not inp.get("group") or o["group"] == inp["group"]) and all(w in f"{o['id']} {o['summary']}".lower() for w in q)]
        return await _run(go)

    async def read(inp, **_):
        op_id = str(inp.get("operation") or "")

        def go():
            if client.operation(op_id)["mutating"]:
                raise ManagerError(f"{op_id} changes something; use hm_call for it")
            return client.call(op_id, dict(inp.get("args") or {}))
        return await _run(go)

    async def call(inp, **_):
        op_id = str(inp.get("operation") or "")
        if not op_id:
            return "Error: give the operation id (see hm_operations)"
        return await _run(client.call, op_id, dict(inp.get("args") or {}))

    def gui(allowed: set[str]):
        async def handler(inp, **_):
            action = str(inp.get("action") or "")
            if action not in allowed:
                return f"Error: action is one of {', '.join(sorted(allowed))}"
            args = {k: v for k, v in inp.items() if k != "action" and v is not None}
            if action == "launch":
                return await _run(harness.open_window, float(args.get("wait_s") or 60))
            return await _run(client.call, f"gui.{action}", args)
        return handler

    async def setup(inp, **_):
        fns = {"install": harness.setup, "update": harness.update, "start": harness.start_bridge,
               "stop": harness.stop_bridge, "jobs": harness.jobs, "register_mcp": harness.register_mcp}
        action = str(inp.get("action") or "")
        if action not in fns:
            return f"Error: action is one of {', '.join(fns)}"
        return await _run(fns[action])

    target = {"type": "object", "description": "{id} (its data-testid or the id hm_gui_look inspect gave it) or {text} "
                                               "(its visible text, label or placeholder), optionally {role}",
              "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "role": {"type": "string"}}}

    reg("hm_status", "Hermes Manager at a glance: installed and built, commit and updates waiting, whether its bridge "
        "and window are running, and Hermes's own health (install, gateway, data sources).", {}, [], status,
        permission="read", read_only=True)
    reg("hm_operations", "Search Hermes Manager's operations: gateway (status, fleet, processes, start/stop/restart/drain), "
        "logs, sessions (and transcripts), chat, config (read, diff, apply config.yaml and .env), updates, backups "
        "(create, preview, restore, delete), tools (MCP servers, skills, cron, plugins, models), gui. With operation: its "
        "arguments.", {"query": {"type": "string"}, "group": {"type": "string"}, "operation": {"type": "string"}}, [],
        operations, permission="read", read_only=True)
    reg("hm_read", "Run a Hermes Manager operation that changes nothing (gateway.status, logs.tail, sessions.sessions, "
        "config.read_config, tools.skills_list, updates.report...). Refuses the ones that change something.",
        {"operation": {"type": "string"}, "args": {"type": "object"}}, ["operation"], read, permission="read", read_only=True)
    reg("hm_call", "Run any Hermes Manager operation by id with its arguments: gateway lifecycle and drain, chat, config "
        "diff/apply, update check/apply, backups, skill toggles, cron actions, MCP server tests (see hm_operations).",
        {"operation": {"type": "string"}, "args": {"type": "object"}}, ["operation"], call, permission="external",
        read_only=False)
    reg("hm_gui_look", "Look at the Hermes Manager window without changing it. action: state, sections, inspect (every "
        "button, field, select, checkbox, tab, table on screen), find (elements matching query), read (one element or "
        "table), text (everything on screen), screenshot, wait (until an element or text appears).",
        {"action": {"type": "string", "enum": sorted(GUI_LOOK)}, "query": {"type": "string"}, "target": target,
         "text": {"type": "string"}, "timeout_s": {"type": "number"}, "max_width": {"type": "integer"}}, ["action"],
        gui(GUI_LOOK), permission="read", read_only=True)
    reg("hm_gui_act", "Drive the Hermes Manager window as a person would. action: launch (open it), open (a section: "
        "overview, logs, gateway, sessions, chat, updates, backups, config, tools), click, fill (a text field; submit "
        "presses Enter), select (an option), check (a checkbox), key, window (show/hide/focus/minimize/maximize/"
        "restore/resize).", {"action": {"type": "string", "enum": sorted(GUI_ACT)}, "section": {"type": "string"},
                             "target": target, "value": {"type": "string"}, "submit": {"type": "boolean"},
                             "option": {"type": "string"}, "checked": {"type": "boolean"}, "keys": {"type": "string"},
                             "width": {"type": "integer"}, "height": {"type": "integer"}, "wait_s": {"type": "number"}},
        ["action"], gui(GUI_ACT), permission="external", read_only=False)
    reg("hm_setup", "Install or maintain Hermes Manager. action: install (clone from its repo if missing, npm install, "
        "build), update (pull and rebuild; refuses over uncommitted work), start / stop (a headless bridge), jobs, "
        "register_mcp (add its MCP server to ABP's external MCP servers).",
        {"action": {"type": "string", "enum": ["install", "update", "start", "stop", "jobs", "register_mcp"]}},
        ["action"], setup, permission="config", read_only=False)


register_tools()
