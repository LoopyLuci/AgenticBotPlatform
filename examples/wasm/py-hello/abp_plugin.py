"""An ABP plugin written in Python, compiled to a component by componentize-py.

The world (`wit/abp-plugin.wit`) is unchanged: a plugin is a plugin whatever language it is
written in. This module implements the two functions the world exports - `describe` and
`invoke` - and reaches the host through the `wit_world.imports.host` bindings that
componentize-py generates from the same WIT.

Build it with:

    pip install componentize-py                 # per-user; nothing needs admin
    componentize-py -d ../../../wit -w plugin componentize abp_plugin -o abp_py_hello.wasm

or, to keep the tooling out of any environment ABP cares about:

    python -m pip install --target <somewhere> componentize-py
    PYTHONPATH=<somewhere> <somewhere>/bin/componentize-py ...

Note what this plugin does *not* import: no sockets, no environment, no filesystem. Every
capability it has is one it asked for in `describe` and a person approved - here, none at
all, which is why it installs without a conversation.
"""

from __future__ import annotations

import json

from wit_world.imports import host

#: The one tool this plugin offers, with the JSON Schema for its arguments. ABP hands the
#: schema to the model verbatim, so it is a real schema rather than prose.
GREET_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "who to greet"},
            "greeting": {"type": "string", "description": "the greeting to use; defaults to Hello"},
        },
        "required": ["name"],
    }
)


class Guest:
    """What the world's `abp:plugin/guest@0.1.0` interface exports.

    componentize-py generates the bindings for the world from the same WIT and calls these
    two; the class name is the interface's, because that is what it looks for.
    """

    @staticmethod
    def describe() -> dict:
        """Who this plugin is and what it can do. ABP reads this at install time, before
        anything is approved - so it is cheap, and it says what the plugin wants as well as
        what it does."""
        return {
            "name": "py-hello",
            "version": "1.0.0",
            "description": "Greets someone. The same plugin as the Rust example, in Python.",
            "tools": [
                {
                    "name": "greet",
                    "description": "Greet someone by name, optionally with a greeting of your choosing",
                    "schema": GREET_SCHEMA,
                }
            ],
            # No preopens and no network: this plugin asks for nothing, so there is nothing
            # for a person to approve and nothing it can reach.
            "preopens": [],
            "network": [],
        }

    @staticmethod
    def invoke(tool: str, args_json: str) -> tuple:
        """Run one tool. Returns `(result_json, "")` for success and `("", message)` for a
        failure the caller should see - which is what the world's `result<string, string>` is
        for. An empty error string means the ok value is the result."""
        if tool != "greet":
            return "", f"this plugin has one tool, greet; {tool!r} is not one of them"
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError as exc:
            return "", f"the arguments are not JSON: {exc}"
        if not isinstance(args, dict):
            return "", "greet's arguments must be an object"
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return "", 'greet needs a "name" string'
        greeting = args.get("greeting")
        if not isinstance(greeting, str) or not greeting:
            greeting = "Hello"
        host.log("info", f"greeted {name}")
        return json.dumps({"greeting": f"{greeting}, {name}!"}), ""
