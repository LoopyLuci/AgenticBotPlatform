"""python -m abp_run [PROMPT | -] --model provider/model [options]

Runs the ABP agent once, without a chat channel, and prints the answer. For scripts, CI and editors.

  PROMPT               the task; "-" (or no PROMPT with input piped in) reads it from standard input
  --model REF          provider/model, e.g. anthropic/claude-sonnet-5 or openrouter/qwen/qwen3.8-27b:free
                       (default: $ABP_RUN_MODEL)
  --cwd DIR            the working folder the agent may use (default: here)
  --permission-mode M  plan (read-only) | default | accept_edits | bypass   (default: the configured mode)
  --approve deny|allow what to do when a tool needs a person's approval; nobody is present, so the default
                       is deny. "allow" approves everything the permission rules would ask about, except in a
                       session that read untrusted web content (those are still refused).
  --json               print one JSON object (reply, tokens, tool calls, run id, exit code) instead of text
  --stream             print the answer as it is written (text mode only)
  --timeout SECONDS    give up after this long (default 600)
  --persist            use the real database and trace store instead of a throwaway one

Exit status: 0 done | 1 the run failed | 2 bad usage | 3 stopped at a step, time or token limit |
4 the model's rate limit or allowance is used up (see docs/agents/models.md)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="abp_run", description=__doc__.splitlines()[0])
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--model", default=os.environ.get("ABP_RUN_MODEL", ""))
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--permission-mode", choices=["plan", "default", "accept_edits", "bypass"], default=None)
    ap.add_argument("--approve", choices=["deny", "allow"], default="deny")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--persist", action="store_true")
    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    prompt = args.prompt
    if prompt in (None, "-"):
        if prompt is None and sys.stdin.isatty():
            print("give a PROMPT, or pipe one in", file=sys.stderr)
            return core.EXIT_USAGE
        prompt = sys.stdin.read()
    prompt = (prompt or "").strip()
    if not prompt:
        print("the prompt is empty", file=sys.stderr)
        return core.EXIT_USAGE
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        print(f"--cwd {cwd} is not a folder", file=sys.stderr)
        return core.EXIT_USAGE
    try:
        provider, model = core.split_model(args.model)
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return core.EXIT_USAGE

    streamed = []

    async def on_text(text: str) -> None:
        streamed.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()

    try:
        result = core.run_once(prompt, provider=provider, model=model, cwd=cwd, approve=args.approve,
                               permission_mode=args.permission_mode, timeout_s=args.timeout, persist=args.persist,
                               on_text=on_text if (args.stream and not args.json) else None)
    except core.RunError as exc:
        print(str(exc), file=sys.stderr)
        return core.EXIT_USAGE
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        if args.stream and streamed:
            print()
        else:
            print(result.reply if result.ok else f"error: {result.error}")
        if not result.ok and args.stream and streamed:
            print(f"error: {result.error}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
