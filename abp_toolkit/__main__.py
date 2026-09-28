"""The toolkit from the command line, and as an MCP server. See abp_toolkit/__init__.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from abp_toolkit.registry import GROUPS, ToolkitError, call, catalog, get, load_all


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    cmd = argv[0] if argv else "help"
    if cmd == "list":
        load_all()
        group = argv[1] if len(argv) > 1 else ""
        for g, summary in sorted(GROUPS.items()):
            if group and g != group:
                continue
            print(f"\n{g}: {summary}")
            for a in catalog(g):
                flags = "".join(c for c, on in (("w", a["writes"]), ("x", a["executes"]), ("n", a["network"])) if on)
                print(f"  {a['id']:<24} {flags:<3} {a['summary']}")
        print("\nw = writes files   x = runs programs   n = uses the network")
        return 0
    if cmd == "describe" and len(argv) > 1:
        print(json.dumps(get(argv[1]).describe(), indent=2))
        return 0
    if cmd == "call" and len(argv) > 1:
        args: dict = {}
        rest = argv[2:]
        i = 0
        while i < len(rest):
            if rest[i] == "--json":
                args.update(json.loads(rest[i + 1]))
                i += 2
                continue
            k, sep, v = rest[i].partition("=")
            if not sep:
                print(f"arguments are key=value; got {rest[i]!r}", file=sys.stderr)
                return 2
            args[k] = _value(v)
            i += 1
        try:
            print(json.dumps(call(argv[1], args, workspace=Path.cwd()), indent=2, ensure_ascii=False, default=str))
        except ToolkitError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    if cmd == "mcp":
        from abp_toolkit.mcp import main as mcp_main
        return mcp_main(argv[1:])
    print(__import__("abp_toolkit").__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
