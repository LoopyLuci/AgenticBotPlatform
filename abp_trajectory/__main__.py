"""python -m abp_trajectory export --out runs.jsonl --confirm [options]

Write finished agent runs as JSONL chat transcripts (see bot/agent_runtime/trajectory.py for the format and the cautions).
Refuses without --confirm: the transcripts contain conversation content.

  --only-ok / --all        only runs that finished normally (default) or every run
  --min-tool-calls N       skip runs that used fewer tools
  --exclude-denied         skip runs where a tool call was denied
  --model M                only this model (repeatable)
  --max-tool-chars N       shorten tool output to this many characters (default 2000)
  --scrub-pii              also mask e-mail addresses, phone numbers and IP addresses
  --limit N                look at the newest N runs' worth of events (default 1000)"""
from __future__ import annotations

import argparse
import sys

from bot.agent_runtime import trajectory


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_trajectory", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--out", required=True)
    ex.add_argument("--confirm", action="store_true", help="acknowledge that the output contains conversation content")
    ex.add_argument("--all", action="store_true")
    ex.add_argument("--min-tool-calls", type=int, default=0)
    ex.add_argument("--exclude-denied", action="store_true")
    ex.add_argument("--model", action="append", default=[])
    ex.add_argument("--max-tool-chars", type=int, default=trajectory.MAX_TOOL_CHARS_DEFAULT)
    ex.add_argument("--scrub-pii", action="store_true")
    ex.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args(argv)
    if not args.confirm:
        print("Refusing: the export contains people's messages and tool output. Re-run with --confirm if you have the right to use them.", file=sys.stderr)
        return 2
    records = trajectory.export(only_ok=not args.all, min_tool_calls=args.min_tool_calls, exclude_denied=args.exclude_denied, models=args.model,
                                max_tool_chars=args.max_tool_chars, pii=args.scrub_pii, limit=args.limit)
    n = trajectory.write_jsonl(records, args.out)
    print(f"wrote {n} run(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
