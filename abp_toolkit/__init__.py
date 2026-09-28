"""ABP's toolkit for programming, computer science and asset work, usable by any model or agent.

    python -m abp_toolkit list [group]                   every action
    python -m abp_toolkit describe lint.check            one action's arguments
    python -m abp_toolkit call lint.check path=src       run one (key=value or --json '{...}'), in the current folder
    python -m abp_toolkit mcp                            the whole kit as an MCP server (stdio)

Inside ABP the agent gets one tool per group (toolkit_lint, toolkit_format, toolkit_run, toolkit_analyze,
toolkit_python, toolkit_cs, toolkit_generate, toolkit_asset, toolkit_scripts). See docs/agents/toolkit.md.
"""
from abp_toolkit.registry import ACTIONS, GROUPS, ToolkitError, call, catalog, get, load_all

__all__ = ["ACTIONS", "GROUPS", "ToolkitError", "call", "catalog", "get", "load_all"]
