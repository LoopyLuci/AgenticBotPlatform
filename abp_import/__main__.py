"""python -m abp_import {claude-code|opencode|hermes|openclaw} [--project DIR] [--user-home DIR] [--source DIR] [--apply]

Read another agent product's settings and set up the ABP equivalents. Prints a plan and changes nothing unless --apply is given.
See abp_import/core.py (Claude Code, OpenCode) and abp_import/agents.py (Hermes, OpenClaw) for exactly what is and is not
imported."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import agents, core

SOURCES = {**core.IMPORTERS, "hermes": agents.hermes, "openclaw": agents.openclaw}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_import", description=__doc__.splitlines()[0])
    ap.add_argument("product", metavar="source", choices=sorted(SOURCES))
    ap.add_argument("--project", default=".", help="the project folder to read (default: here)")
    ap.add_argument("--user-home", default=str(Path.home()), help="the folder holding the user-level settings (default: your home)")
    ap.add_argument("--source", dest="source_dir", default=None, help="hermes / openclaw: their data folder (default: found automatically)")
    ap.add_argument("--instance", type=int, default=None,
                    help="hermes / openclaw: put the instructions, memories and jobs on this existing bot instead of a new one")
    ap.add_argument("--no-secrets", action="store_true", help="hermes / openclaw: import no API key or chat token")
    ap.add_argument("--apply", action="store_true", help="write the plan into ABP (default: only print it)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # another product's data can hold characters a Windows console cannot show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    project, home = Path(args.project).expanduser().resolve(), Path(args.user_home).expanduser().resolve()
    if args.product in ("hermes", "openclaw"):
        plan = SOURCES[args.product](project, home, Path(args.source_dir).expanduser().resolve() if args.source_dir else None)
    else:
        plan = SOURCES[args.product](project, home)
    print(core.render(plan))
    if not args.apply:
        print("\nDry run. Nothing was changed; add --apply to write this into ABP.")
        return 0
    if plan.empty():
        return 0
    try:
        done = core.apply(plan, instance_id=args.instance, with_secrets=not args.no_secrets)
    except (PermissionError, ValueError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    bot_id = done.pop("bot_id", None)
    counts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in done.items() if v)
    print("\nApplied: " + counts if counts else "\nNothing new to apply.")
    if bot_id is not None:
        print(f"Instructions, memories and jobs are on bot {bot_id}. Chat bots were created switched off, and jobs paused: "
              "review them on the Bots page, then switch them on.")
    for w in plan.warnings[-10:]:
        if "already" in w or "not created" in w or "not written" in w:
            print(f"  note   {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
