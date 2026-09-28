"""The toolkit (abp_toolkit) as agent tools: one tool per group for the actions that only look, and one ``..._act``
tool for the actions that write files, run programs or use the network, which ask for approval like any other change.

    toolkit_lint / toolkit_lint_act          lint any language; apply linters' safe fixes
    toolkit_format / toolkit_format_act      format a snippet; format files, line endings, whitespace
    toolkit_run / toolkit_run_act            which languages run here; run snippets, files, commands
    toolkit_analyze                          metrics, complexity, imports, duplicates, unused code, risks, TODOs, outline
    toolkit_python / toolkit_python_act      interpreter info, AST, safe eval; venvs, pip, tests, types, profiling, build
    toolkit_cs / toolkit_cs_act              regex, encodings, hashes, numbers, diffs, JSON, time, SQL, graphs, cron, ids; big-O
    toolkit_generate / toolkit_generate_act  templates; create projects and project files
    toolkit_asset / toolkit_asset_act        palettes, contrast, image info, banners; icons, images, charts, diagrams, QR...
    toolkit_scripts / toolkit_scripts_act    browse the script library; run or install a script

File paths are relative to the agent's working folder and cannot leave it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

MAX_OUT = 16_000


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def register_tools() -> None:
    try:
        from abp_toolkit import registry
    except ImportError:          # an install without the toolkit: no toolkit tools, everything else unaffected
        return
    from bot.agent_runtime import toolspec

    registry.load_all()
    for group, summary in sorted(registry.GROUPS.items()):
        actions = [a for a in registry.ACTIONS.values() if a.group == group]
        for acting in (False, True):
            chosen = sorted((a for a in actions if (not a.read_only) == acting), key=lambda a: a.id)
            if not chosen:
                continue
            name = f"toolkit_{group}" + ("_act" if acting else "")
            names = [a.id.split(".", 1)[1] for a in chosen]
            lines = "\n".join(f"- {a.id.split('.', 1)[1]}: {a.summary}" for a in chosen)
            schema = {"name": name,
                      "description": (f"{summary}" + (" (these change files or run programs)" if acting else "") +
                                      f".\nActions:\n{lines}\nPut the action's arguments in args; toolkit_describe "
                                      "gives any action's full argument schema. Paths are relative to your working folder."),
                      "input_schema": {"type": "object", "properties": {
                          "action": {"type": "string", "enum": names},
                          "args": {"type": "object", "description": "the action's arguments"}}, "required": ["action"]}}

            def make(group: str = group, allowed: frozenset = frozenset(names)):
                async def handler(inp: dict, *, workspace=None, **_: Any) -> str:
                    action = str(inp.get("action") or "")
                    if action not in allowed:
                        return f"Error: action is one of {', '.join(sorted(allowed))}"
                    args = inp.get("args") if isinstance(inp.get("args"), dict) else {}
                    try:
                        result = await asyncio.to_thread(registry.call, f"{group}.{action}", args,
                                                         workspace=Path(workspace) if workspace else None)
                    except registry.ToolkitError as exc:
                        return f"Error: {exc}"
                    return _out(result)
                return handler

            permission = "read" if not acting else ("execute" if any(a.executes for a in chosen) else "write")
            toolspec.register(schema, toolspec.ToolSpec(name, permission, read_only=not acting, concurrency_safe=not acting,
                                                        origin="registered"), make())

    async def describe(inp: dict, **_: Any) -> str:
        try:
            return _out(registry.get(str(inp.get("action") or "")).describe())
        except registry.ToolkitError as exc:
            return f"Error: {exc}"

    toolspec.register({"name": "toolkit_describe", "description": "The full argument schema of a toolkit action, e.g. lint.check or asset.icon.",
                       "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
                      toolspec.ToolSpec("toolkit_describe", "read", read_only=True, concurrency_safe=True, origin="registered"),
                      describe)


register_tools()
