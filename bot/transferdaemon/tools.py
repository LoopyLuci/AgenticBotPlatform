"""The agent's TransferDaemon tools. Offered while TransferDaemon is installed (or a server is linked). Reading is free;
anything that sends, changes contacts or settings, or drives the window or terminal UI asks first. Every tool takes
`machine`: a linked server's name, to do the same on that machine through its ABP.

    td_status       installed? built? running? this identity, contacts, transfers, window/TUI, relays
    td_operations   search TransferDaemon's operations (80), or one's arguments
    td_read         run any operation that changes nothing (contacts, messages, transfers, connections, telemetry...)
    td_call         run any operation: send messages and files, manage contacts and groups, settings, relays (asks first)
    td_send         send a message or a file to a contact by name (asks first)
    td_gui_look     the window: state, every widget on screen, a screenshot, wait for text
    td_gui_act      drive the window: open it, navigate, open a chat, click, fill in, type, keys (asks first)
    td_tui          the terminal UI: open it (headless or in a console), read its screen, press keys, type (asks first)
    td_setup        install / update TransferDaemon from its repo, start or stop the daemon, add its MCP server (asks first)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from bot.transferdaemon import client, harness
from bot.transferdaemon.client import DaemonError

MAX_OUT = 12_000
API = "/api/transferdaemon"
GUI_LOOK = {"state", "pages", "inspect", "find", "screenshot", "wait"}
GUI_ACT = {"launch", "navigate", "open_chat", "click", "set", "type", "key", "scroll", "window"}
TUI_ACTIONS = {"launch", "state", "screen", "key", "type", "navigate", "resize", "quit"}
MACHINE = {"type": "string", "description": "a linked server's name (Peers page) to do this on that machine instead of "
                                            "this one; its owner must allow it (peers.remote_control: [transferdaemon])"}


def _out(value: Any) -> str:
    if isinstance(value, dict) and value.get("format") == "png" and "base64" in value:
        value = {**{k: v for k, v in value.items() if k != "base64"},
                 "note": "PNG captured; the dashboard's TransferDaemon page shows it"}
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except DaemonError as exc:
        return f"Error ({exc.code}): {exc}"


async def _remote(machine: str, method: str, path: str, body: Any = None, *, shape=None) -> str:
    """The same thing on a linked server (its ABP must allow transferdaemon: peers.remote_control)."""
    from bot import peers
    try:
        data = await peers.on_machine(machine, method, API + path, body)
        return _out(shape(data) if shape else data)
    except (peers.PeerError, DaemonError) as exc:
        return f"Error: {exc}"


def _result(d: Any) -> Any:
    return d.get("result") if isinstance(d, dict) and "result" in d else d


def _filter_ops(ops: list, inp: dict) -> Any:
    q = str(inp.get("query") or "").lower().split()
    group = str(inp.get("group") or "")
    if not q and not group:
        counts: dict[str, int] = {}
        for o in ops:
            counts[o["group"]] = counts.get(o["group"], 0) + 1
        return {"groups": counts, "hint": "pass query or group to list operations, operation for one's arguments"}
    return [{"id": o["id"], "summary": o["summary"], "changes": o["mutating"], "destructive": o["destructive"]}
            for o in ops if (not group or o["group"] == group) and all(w in f"{o['id']} {o['summary']}".lower() for w in q)]


def _find_op(ops: list, op_id: str) -> dict:
    for o in ops:
        if o["id"] == op_id:
            return o
    raise DaemonError(f"no operation {op_id!r}", code="not_found")


def _contact_id(ref: str) -> str:
    contacts = (client.call("contacts.get_contacts") or {}).get("contacts") or []
    r = ref.strip().lower()
    for c in contacts:
        if c["id"] == ref or str(c.get("name", "")).lower() == r:
            return c["id"]
    hits = [c for c in contacts if c["id"].startswith(ref) or r in str(c.get("name", "")).lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    names = ", ".join(str(c.get("name")) for c in contacts) or "none"
    raise DaemonError(f"no single contact matches {ref!r} (contacts: {names})", code="not_found")


_enabled_cache: dict[str, float | bool] = {"at": 0.0, "on": False}


def _enabled() -> bool:
    """Installed here (or a linked server may have it). Checked every turn: files only, cached 30 s."""
    if time.monotonic() - float(_enabled_cache["at"]) < 30:
        return bool(_enabled_cache["on"])
    try:
        on = harness._is_checkout(harness.install_dir()) or bool(client._cfg().get("url"))
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

    async def status(inp, **_):
        if inp.get("machine"):
            return await _remote(inp["machine"], "GET", "/status")
        return await _run(harness.summary)

    async def operations(inp, **_):
        if inp.get("machine"):
            return await _remote(inp["machine"], "GET", "/operations",
                                 shape=lambda ops: _find_op(ops, str(inp["operation"])) if inp.get("operation")
                                 else _filter_ops(ops, inp))
        return await _run(lambda: client.operation(str(inp["operation"])) if inp.get("operation")
                          else _filter_ops(client.operations(), inp))

    async def read(inp, **_):
        op_id = str(inp.get("operation") or "")
        args = dict(inp.get("args") or {})
        if inp.get("machine"):
            from bot import peers
            try:
                op = _find_op(await peers.on_machine(inp["machine"], "GET", API + "/operations"), op_id)
            except (peers.PeerError, DaemonError) as exc:
                return f"Error: {exc}"
            if op["mutating"]:
                return f"Error: {op_id} changes something; use td_call for it"
            return await _remote(inp["machine"], "POST", "/call", {"operation": op_id, "args": args}, shape=_result)

        def go():
            if client.operation(op_id)["mutating"]:
                raise DaemonError(f"{op_id} changes something; use td_call for it", code="mutating")
            return client.call(op_id, args)
        return await _run(go)

    async def call(inp, **_):
        op_id = str(inp.get("operation") or "")
        if not op_id:
            return "Error: give the operation id (see td_operations)"
        args = dict(inp.get("args") or {})
        if inp.get("machine"):
            return await _remote(inp["machine"], "POST", "/call", {"operation": op_id, "args": args}, shape=_result)
        return await _run(client.call, op_id, args)

    async def send(inp, **_):
        to = str(inp.get("to") or "")
        text, path = inp.get("text"), inp.get("file")
        if not to or not (text or path):
            return "Error: give to (a contact's name or id) and text or file"
        if inp.get("machine"):
            return await _remote(inp["machine"], "POST", "/send", {"to": to, "text": text, "file": path})

        def go():
            cid = _contact_id(to)
            if path:
                return client.call("transfers.send_file", {"contact_id": cid, "file_path": str(path)})
            return client.call("messages.send_text", {"contact_id": cid, "text": str(text)})
        return await _run(go)

    def gui(allowed: set[str]):
        async def handler(inp, **_):
            action = str(inp.get("action") or "")
            if action not in allowed:
                return f"Error: action is one of {', '.join(sorted(allowed))}"
            args = {k: v for k, v in inp.items() if k not in ("action", "machine") and v is not None}
            if inp.get("machine"):
                return await _remote(inp["machine"], "POST", "/call", {"operation": f"gui.{action}", "args": args},
                                     shape=_result)
            if action == "launch":
                return await _run(harness.open_window, float(args.get("wait_s") or 60))
            return await _run(client.call, f"gui.{action}", args, timeout=float(args.get("timeout_s") or 60) + 30)
        return handler

    async def tui(inp, **_):
        action = str(inp.get("action") or "")
        if action not in TUI_ACTIONS:
            return f"Error: action is one of {', '.join(sorted(TUI_ACTIONS))}"
        args = {k: v for k, v in inp.items() if k not in ("action", "machine") and v is not None}
        if inp.get("machine"):
            return await _remote(inp["machine"], "POST", "/call", {"operation": f"tui.{action}", "args": args}, shape=_result)
        if action == "screen":
            return await _run(lambda: (client.call("tui.screen", {}) or {}).get("text", ""))
        return await _run(client.call, f"tui.{action}", args, timeout=120)

    async def setup(inp, **_):
        fns = {"install": harness.setup, "update": harness.update, "start": harness.start_daemon, "stop": harness.stop_daemon,
               "jobs": harness.jobs, "register_mcp": harness.register_mcp}
        action = str(inp.get("action") or "")
        if action not in fns:
            return f"Error: action is one of {', '.join(fns)}"
        if inp.get("machine"):
            routes = {"install": ("POST", "/setup"), "update": ("POST", "/update"), "start": ("POST", "/daemon/start"),
                      "stop": ("POST", "/daemon/stop"), "jobs": ("GET", "/jobs"), "register_mcp": ("POST", "/mcp")}
            return await _remote(inp["machine"], *routes[action])
        return await _run(fns[action])

    target = {"type": "object", "description": "a widget: {id} (from td_gui_look inspect), {text} (its label or text, or "
                                               "what an icon button means: send, attach file, voice call, back...), or "
                                               "{role, index} (e.g. the chat's message box: role multiline_text_input)",
              "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "role": {"type": "string"},
                             "index": {"type": "integer"}}}

    reg("td_status", "TransferDaemon at a glance: installed and built, commit and updates waiting, whether the daemon runs, "
        "this device's identity, how many contacts and transfers, whether its window and terminal UI are open, relays.",
        {}, [], status, permission="read", read_only=True)
    reg("td_operations", "Search TransferDaemon's operations: account (identity), contacts (add, rename, block, address, "
        "safety numbers), groups, messages (send, search, reactions, typing), transfers (send files, pause, resume, "
        "cancel), connections (network paths and policy), settings, calls, telemetry, updates, daemon, gui (the window), "
        "tui (the terminal UI), relay (local relays, probes). With operation: its arguments.",
        {"query": {"type": "string"}, "group": {"type": "string"}, "operation": {"type": "string"}}, [], operations,
        permission="read", read_only=True)
    reg("td_read", "Run a TransferDaemon operation that changes nothing (contacts.get_contacts, messages.get_messages, "
        "messages.search_messages, transfers.get_transfers, connections.list_connections, telemetry.get_snapshot, "
        "daemon.status, relay.status...). Refuses the ones that change something.",
        {"operation": {"type": "string"}, "args": {"type": "object"}}, ["operation"], read, permission="read", read_only=True)
    reg("td_call", "Run any TransferDaemon operation by id with its arguments: send messages and files, add or change "
        "contacts and groups, set a contact's address, change settings or connection policy, start relays, calls, "
        "updates (see td_operations).", {"operation": {"type": "string"}, "args": {"type": "object"}}, ["operation"], call,
        permission="external", read_only=False)
    reg("td_send", "Send an end-to-end encrypted message (text) or a file (file: a path on that machine) to a contact by "
        "name or id. If the contact is offline it is delivered when they are back.",
        {"to": {"type": "string"}, "text": {"type": "string"}, "file": {"type": "string"}}, ["to"], send,
        permission="external", read_only=False)
    reg("td_gui_look", "Look at the TransferDaemon window without changing it. action: state (page, tab, open chat, "
        "identity, counts), pages, inspect (every widget on screen: id, role, label, value, position), find (widgets "
        "matching query), screenshot, wait (until text appears, or is gone).",
        {"action": {"type": "string", "enum": sorted(GUI_LOOK)}, "query": {"type": "string"}, "text": {"type": "string"},
         "gone": {"type": "boolean"}, "timeout_s": {"type": "number"}, "max_width": {"type": "integer"}}, ["action"],
        gui(GUI_LOOK), permission="read", read_only=True)
    reg("td_gui_act", "Drive the TransferDaemon window as a person would. action: launch (open it), navigate (to: chats, "
        "groups, contacts, transfers, settings, telemetry, connections), open_chat (contact or group), click, set (a text "
        "field's value; submit presses Enter), type, key (keys: Enter, Escape, ctrl+N...), scroll (dy), window "
        "(show/focus/minimize/maximize/restore/resize/close).",
        {"action": {"type": "string", "enum": sorted(GUI_ACT)}, "to": {"type": "string"}, "contact": {"type": "string"},
         "group": {"type": "string"}, "target": target, "value": {"type": "string"}, "submit": {"type": "boolean"},
         "text": {"type": "string"}, "keys": {"type": "string"}, "double": {"type": "boolean"}, "dy": {"type": "number"},
         "width": {"type": "number"}, "height": {"type": "number"}, "wait_s": {"type": "number"}},
        ["action"], gui(GUI_ACT), permission="external", read_only=False)
    reg("td_tui", "TransferDaemon's terminal UI. action: launch (headless: drawn in memory and driven from here, the "
        "default; or headless=false for a console window), state, screen (the text on it), key (keys: F1-F5 switch tabs, "
        "Tab moves focus, Enter sends, Esc, Up/Down, ctrl+Q quits), type (text), navigate (to: chats, contacts, "
        "transfers, settings, telemetry), resize (headless), quit.",
        {"action": {"type": "string", "enum": sorted(TUI_ACTIONS)}, "headless": {"type": "boolean"},
         "width": {"type": "integer"}, "height": {"type": "integer"}, "keys": {"type": "string"}, "text": {"type": "string"},
         "to": {"type": "string"}}, ["action"], tui, permission="external", read_only=False)
    reg("td_setup", "Install or maintain TransferDaemon. action: install (clone it from its repo if missing and build it "
        "with cargo), update (pull the latest and rebuild; refuses over uncommitted work), start / stop (the daemon), "
        "jobs (install/update progress), register_mcp (add its MCP server, transferd-cli mcp, to ABP's external MCP "
        "servers).", {"action": {"type": "string", "enum": ["install", "update", "start", "stop", "jobs", "register_mcp"]}},
        ["action"], setup, permission="config", read_only=False)


register_tools()
