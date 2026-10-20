#!/usr/bin/env python
"""The owner's side of the pipeline's ML diff-lint (bot/conductor/difflint.py).

    python scripts/difflint.py --list               the newest run's findings (id, category, verified or not)
    python scripts/difflint.py --apply <id>         apply that verified fix to the working tree (marks it accepted); commit it yourself
    python scripts/difflint.py --dismiss <id>       the finding was wrong or not worth it (counts against its category's precision)
    python scripts/difflint.py --stats              per-category counts and precision (accepted / judged)
    python scripts/difflint.py --run [--base REF]   run the stage by hand on REF..HEAD (default: the merge base with origin/main)
                                    [--start-hub]   ... starting a hub for the model call when none runs (as the pipeline does)

Nothing here commits, pushes or touches anything but the working tree --apply is asked to change.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.conductor import difflint  # noqa: E402

LOG_DIR = ROOT / "logs" / "local_pipeline"


def _list() -> int:
    runs = sorted(LOG_DIR.glob("*-difflint.json"), reverse=True)
    if not runs:
        print("no diff-lint run yet")
        return 0
    data = json.loads(runs[0].read_text(encoding="utf-8"))
    print(f"{runs[0].name}: {data.get('status')} {data.get('model', '')} {str(data.get('base', ''))[:10]}..{str(data.get('head', ''))[:10]}")
    for f in data.get("findings") or []:
        state = "fix verified" if f.get("verified") else ("fix dropped" if f.get("patch") else "no fix")
        print(f"  {f['id']}  [{f['category']}/{f['severity']}] {f['file']}:{f['line']}  ({state}, {f.get('source', 'model')})")
        print(f"      {f['explanation'][:300]}")
        if f.get("check"):
            print(f"      {f['check'][:200]}")
    return 0


def _stats() -> int:
    data = difflint.load_stats()
    prec = difflint.precision()
    print(f"{'category':<12} {'found':>6} {'verified':>9} {'accepted':>9} {'dismissed':>10} {'precision':>10}")
    for cat, row in sorted(data["categories"].items()):
        p = prec.get(cat)
        print(f"{cat:<12} {row['found']:>6} {row['verified']:>9} {row['accepted']:>9} {row['dismissed']:>10} "
              f"{'-' if p is None else f'{p:.0%}':>10}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", metavar="ID", help="apply a verified fix to the working tree (marks it accepted)")
    g.add_argument("--dismiss", metavar="ID", help="mark a finding dismissed")
    g.add_argument("--list", action="store_true", help="the newest run's findings")
    g.add_argument("--stats", action="store_true", help="per-category counts and precision")
    g.add_argument("--run", action="store_true", help="run the stage by hand")
    ap.add_argument("--base", default="", help="with --run: the base ref (default: merge-base HEAD origin/main)")
    ap.add_argument("--start-hub", action="store_true", help="with --run: start a hub for the model call when none runs")
    args = ap.parse_args(argv)
    if args.list:
        return _list()
    if args.stats:
        return _stats()
    if args.dismiss:
        try:
            difflint.mark(args.dismiss, "dismissed")
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 1
        print(f"{args.dismiss}: dismissed")
        return 0
    if args.apply:
        try:
            files = difflint.apply_patch(args.apply, ROOT, LOG_DIR)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{args.apply}: applied to the working tree ({files}) and marked accepted - review with `git diff`, then commit it yourself")
        return 0
    base = args.base or subprocess.run(["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], capture_output=True,
                                       text=True).stdout.strip()
    if not base:
        print("no base: pass --base REF", file=sys.stderr)
        return 1
    rep = difflint.run(ROOT, base, "HEAD", start_hub=args.start_hub)
    print(rep.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
