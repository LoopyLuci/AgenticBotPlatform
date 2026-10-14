"""Command line: python -m bot.codeintel <command> [--root DIR]

    index [--force] [--chunks]   build or update the index and print timings
    outline PATH                 signatures and line ranges of a file or folder
    find QUERY [--kind K]        find symbols
    read TARGET [--context N]    one symbol's source (qualified name, path::name or path:line)
    refs SYMBOL                  where it is used
    callers SYMBOL               who calls it
    tests TARGET                 tests for a file or symbol
    impact [PATH ...|-]          symbols, callers and tests touched; with no argument, `git diff` of the root
    pack TASK [--budget N]       a budgeted context bundle for a task
    stats                        index size, parsers, languages, per-tool token counters
    bench                        tokens: grep + read_file versus these tools, for questions about ABP's own code
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bot.codeintel import tools


def _git_diff(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "diff", "HEAD", "--unified=0"], capture_output=True, timeout=60,
                              check=False).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.codeintel", description="Code navigation with the fewest tokens.")
    ap.add_argument("--root", default=".", help="repository folder (default: the current folder)")
    ap.add_argument("--json", action="store_true", help="print structured output")
    ap.add_argument("--max-tokens", type=int, default=0, help="budget for the answer (0 = the tool's default)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index"); p.add_argument("--force", action="store_true"); p.add_argument("--chunks", action="store_true")
    for name in ("outline", "refs", "callers", "tests", "find", "read", "pack"):
        p = sub.add_parser(name)
        p.add_argument("target", nargs="+" if name == "pack" else 1)
        if name == "find":
            p.add_argument("--kind", default="")
        if name == "read":
            p.add_argument("--context", type=int, default=0)
        if name == "pack":
            p.add_argument("--budget", type=int, default=4000)
    p = sub.add_parser("impact"); p.add_argument("paths", nargs="*")
    sub.add_parser("stats")
    sub.add_parser("bench")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    kw = {"max_tokens": args.max_tokens} if args.max_tokens else {}

    try:
        if args.cmd == "index":
            from bot.codeintel import indexer

            out = indexer.update(root, force=args.force, chunks=args.chunks)
            print(json.dumps(out) if args.json else
                  f"{out['files']} files, {out.get('symbols', 0)} symbols; {out['indexed']} parsed, {out['unchanged']} unchanged, "
                  f"{out['removed']} removed in {out['seconds']}s" + ("" if out["complete"] else " (time budget hit: run again)"))
            return 0
        if args.cmd == "stats":
            s = tools.stats(root)
            print(json.dumps(s, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in s.items()))
            return 0
        if args.cmd == "bench":
            from bot.codeintel import bench

            report = bench.run(root)
            print(json.dumps(report, indent=2) if args.json else bench.render(report))
            return 0
        if args.cmd == "outline":
            res = tools.outline(root, args.target[0], **kw)
        elif args.cmd == "find":
            res = tools.find_symbol(root, args.target[0], kind=args.kind, **kw)
        elif args.cmd == "read":
            res = tools.read_symbol(root, args.target[0], context_lines=args.context, **kw)
        elif args.cmd == "refs":
            res = tools.references(root, args.target[0], **kw)
        elif args.cmd == "callers":
            res = tools.callers(root, args.target[0], **kw)
        elif args.cmd == "tests":
            res = tools.related_tests(root, args.target[0], **kw)
        elif args.cmd == "impact":
            changes = sys.stdin.read() if args.paths == ["-"] else ("\n".join(args.paths) if args.paths else _git_diff(root))
            res = tools.impact(root, changes, **kw)
        else:
            res = tools.context_pack(root, " ".join(args.target), budget_tokens=args.max_tokens or args.budget)
    except tools.CodeIntelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"text": res.text, "tokens": res.tokens, "truncated": res.truncated, "data": res.data}) if args.json
          else res.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
