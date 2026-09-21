"""python -m abp_import {claude-code|opencode} [--project DIR] [--user-home DIR] [--apply]

Read another agent product's settings and set up the ABP equivalents. Prints a plan and changes nothing unless --apply is given.
See abp_import/core.py for exactly what is and is not imported."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import core


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_import", description=__doc__.splitlines()[0])
    ap.add_argument("source", choices=sorted(core.IMPORTERS))
    ap.add_argument("--project", default=".", help="the project folder to read (default: here)")
    ap.add_argument("--user-home", default=str(Path.home()), help="the folder holding the user-level settings (default: your home)")
    ap.add_argument("--apply", action="store_true", help="write the plan into ABP (default: only print it)")
    args = ap.parse_args(argv)
    plan = core.IMPORTERS[args.source](Path(args.project).expanduser().resolve(), Path(args.user_home).expanduser().resolve())
    print(core.render(plan))
    if not args.apply:
        print("\nDry run. Nothing was changed; add --apply to write this into ABP.")
        return 0
    if plan.empty():
        return 0
    try:
        done = core.apply(plan)
    except PermissionError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print("\nApplied: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in done.items() if v) or "\nNothing new to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
