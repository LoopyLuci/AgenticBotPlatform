#!/usr/bin/env python
"""A local extension, in the smallest form that still exercises every hook.

It exists so docs/extensions.md has a real example to point at and tests/test_extensions.py
has something to load: a backend, an integration, a dashboard route, a UI panel, an abp_cli
command, an MCP tool and a module entry. Nothing here is ABP's — a greeting, a "what does this
install add" route, and nothing else.
"""
from __future__ import annotations


def register(api) -> None:
    api.add_backend("hello", kind="cli", command="hello --json", description="the hello extension's backend")
    api.add_integration("hello", base_url="http://127.0.0.1:8901", label="Hello")

    def add_routes(app, read_auth, write_auth):
        from fastapi import Depends

        @app.get("/api/hello", dependencies=[Depends(read_auth)])
        async def hello():
            return {"greeting": "hello", "from": api.name}

    api.add_routes(add_routes)

    api.add_panel("hello", "Hello", description="a panel that exists only in this private folder")

    async def hello_cmd(args, client) -> int:
        print("hello from an extension")
        return 0

    api.add_cli_command("hello", hello_cmd, help="say hello (from the hello extension)")

    async def hello_status() -> dict:
        """What the hello extension offers: its own name and version, and nothing that leaves this machine."""
        return {"name": api.name, "version": api.version, "greeting": "hello"}

    api.add_mcp_tool(hello_status, "hello_status")

    api.add_module({"id": "hello", "name": "Hello", "repo": "https://example.invalid/hello.git",
                    "area": "examples", "description": "An example module entry, registered by an extension."})