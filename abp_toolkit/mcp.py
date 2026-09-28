"""The toolkit as an MCP server (stdio): one tool per group, with an `action` argument, so any MCP client (Claude
Desktop, Cursor, VS Code, another agent) gets the whole kit for a dozen tool definitions.

    python -m abp_toolkit mcp [--workspace FOLDER] [--read-only]

File paths are confined to the workspace (default: the folder it was started in). --read-only leaves out every
action that writes files, runs programs or uses the network.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from abp_toolkit.registry import GROUPS, ToolkitError, call, catalog, load_all

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def group_tools(read_only: bool = False) -> list[dict]:
    """One tool per group: its actions as an enum, each action's own arguments under `args`."""
    load_all()
    tools = []
    for g, summary in sorted(GROUPS.items()):
        actions = [a for a in catalog(g) if not read_only or not (a["writes"] or a["executes"] or a["network"])]
        if not actions:
            continue
        lines = [f"- {a['id'].split('.', 1)[1]}: {a['summary']}" + (" [writes files]" if a["writes"] else "")
                 + (" [runs programs]" if a["executes"] else "") + (" [network]" if a["network"] else "") for a in actions]
        tools.append({
            "name": f"toolkit_{g}",
            "description": f"{summary}.\nActions:\n" + "\n".join(lines) + "\nPass the action's arguments in args; "
                           "toolkit_describe gives any action's full argument schema.",
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": [a["id"].split(".", 1)[1] for a in actions]},
                "args": {"type": "object", "description": "the action's arguments"}}, "required": ["action"]},
            "annotations": {"readOnlyHint": all(not (a["writes"] or a["executes"] or a["network"]) for a in actions)},
        })
    tools.append({"name": "toolkit_describe", "description": "The full argument schema of a toolkit action (e.g. lint.check)",
                  "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]},
                  "annotations": {"readOnlyHint": True}})
    return tools


class Server:
    def __init__(self, workspace: Path, read_only: bool = False) -> None:
        self.workspace = workspace
        self.read_only = read_only
        self.tools = group_tools(read_only)
        self.allowed = {a["id"] for a in catalog() if not read_only or not (a["writes"] or a["executes"] or a["network"])}

    async def call_tool(self, name: str, args: dict) -> dict:
        try:
            if name == "toolkit_describe":
                result: Any = next((a for a in catalog() if a["id"] == args.get("action")), None)
                if result is None:
                    raise ToolkitError(f"no action {args.get('action')!r}")
            elif name.startswith("toolkit_"):
                action_id = f"{name[len('toolkit_'):]}.{args.get('action', '')}"
                if action_id not in self.allowed:
                    raise ToolkitError(f"{action_id} is not available here")
                result = await asyncio.to_thread(call, action_id, dict(args.get("args") or {}), workspace=self.workspace)
            else:
                raise ToolkitError(f"unknown tool {name!r}")
        except Exception as e:  # noqa: BLE001 - reported to the model as a tool error
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1, ensure_ascii=False, default=str)[:200_000]}],
                "isError": False}

    async def handle(self, msg: dict) -> Optional[dict]:
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:
            return None
        if method == "initialize":
            v = params.get("protocolVersion", PROTOCOL_VERSIONS[0])
            result: Any = {"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "abp-toolkit", "version": "1.0.0"},
                           "instructions": "Programming, computer science and asset tools: lint any language, format, run code, "
                                           "analyze codebases, Python tooling, regex/encodings/SQL/graphs, project templates, "
                                           "icons/charts/diagrams/sounds, and a script library. Paths are relative to "
                                           f"{self.workspace}."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            result = await self.call_tool(params.get("name", ""), params.get("arguments") or {})
        elif method in ("resources/list", "prompts/list", "resources/templates/list"):
            result = {method.split("/")[0] if method != "resources/templates/list" else "resourceTemplates": []}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


async def serve(server: Server) -> None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    def pump() -> None:
        for raw in sys.stdin.buffer:
            loop.call_soon_threadsafe(lines.put_nowait, raw)
        loop.call_soon_threadsafe(lines.put_nowait, b"")
    threading.Thread(target=pump, daemon=True).start()
    lock = asyncio.Lock()
    pending: set[asyncio.Task] = set()

    async def respond(m: dict) -> None:
        reply = await server.handle(m)
        if reply is not None:
            async with lock:
                sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()

    while True:
        raw = await lines.get()
        if not raw:
            break
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            t = asyncio.create_task(respond(m))
            pending.add(t)
            t.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="abp_toolkit mcp")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--read-only", action="store_true")
    a = ap.parse_args(argv)
    asyncio.run(serve(Server(Path(a.workspace).resolve(), a.read_only)))
    return 0
