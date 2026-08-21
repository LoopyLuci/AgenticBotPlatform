#!/usr/bin/env python
"""Interactive first-run setup — generates a correct, working .env with as
little friction as possible.

Run this before anything else:

    .venv\\Scripts\\python.exe scripts\\setup.py

Safe to re-run any time: fields that are already set and valid are shown
and skipped by default (pass --all to reconfigure everything). Every write
goes through the same backed-up, atomic path the dashboard's .env editor
uses — nothing is ever silently overwritten. The same platform setup here
(Telegram/Discord/Slack) is also reachable any time from the dashboard's
own Platforms settings, not just on first run.

    scripts\\setup.py --check     print status only, exit 1 if incomplete
    scripts\\setup.py --all       reconfigure every field, even valid ones
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import desktop, setup_wizard  # noqa: E402


def _print_status(status: dict) -> None:
    print(f"\n.env location: {status['env_path']}\n")
    for key, field in status["fields"].items():
        mark = "OK  " if field["valid"] else ("--  " if not field["required"] else "MISS")
        print(f"  [{mark}] {field['label']:<32} {field['message']}")
    print("\n  Messaging platforms:")
    for name, p in status["platforms"].items():
        mark = "OK  " if p["configured"] else "--  "
        print(f"  [{mark}] {p['label']}")
    print()
    if status["ready"]:
        print("Ready to run.")
    else:
        reasons = [f["label"] for f in status["fields"].values() if f["required"] and not f["valid"]]
        if not any(p["configured"] for p in status["platforms"].values()):
            reasons.append("at least one messaging platform")
        print("Not ready yet — missing: " + ", ".join(reasons))


def _prompt(label: str, help_text: str, current: str) -> str:
    print(f"\n{label}")
    print(f"  {help_text}")
    if current:
        print(f"  current value is set ({len(current)} chars) — press Enter to keep it")
    raw = input("> ").strip()
    return raw if raw else current


def _prompt_field(key: str, spec: dict, current: str, required: bool) -> Optional[str]:
    while True:
        raw = _prompt(spec["label"], spec["help"], current)
        if not raw:
            if required:
                print("  this one's required — try again.")
                continue
            return None
        ok, msg = spec["validate"](raw)
        if ok:
            return raw
        print(f"  hmm — {msg}. Try again, or Ctrl+C to abort.")


def run_wizard(fields_to_ask: list[str]) -> None:
    values = setup_wizard.current_values()
    collected: dict[str, str] = {}

    for key in fields_to_ask:
        spec = setup_wizard.FIELDS[key]
        current = values.get(key, "")

        if key == "DASHBOARD_TOKEN":
            ok, _ = spec["validate"](current) if current else (False, "")
            if ok:
                print(f"\n{spec['label']}: already set, keeping it.")
                continue
            token = setup_wizard.generate_dashboard_token()
            print(f"\n{spec['label']}: generated one for you.")
            collected[key] = token
            continue

        if key == "CLAUDE_DESKTOP_EXE":
            detected = desktop.find_exe_path()
            if detected and Path(detected).exists():
                print(f"\n{spec['label']}")
                print(f"  auto-detected: {detected}")
                raw = input("  press Enter to accept, or paste a different path (blank line = skip): ").strip()
                collected[key] = raw if raw else detected
            else:
                raw = _prompt_field(key, spec, current, required=False)
                if raw:
                    collected[key] = raw
            continue

        raw = _prompt_field(key, spec, current, required=setup_wizard.is_required(key))
        if raw:
            collected[key] = raw

    if not collected:
        print("\nNothing to change.")
        return

    backup, _status = setup_wizard.apply_setup(collected, actor="setup-wizard-cli")
    print(f"\nSaved.{' Backed up previous version as ' + backup.name + '.' if backup else ''}")


def run_platform_wizard(platform_key: str) -> None:
    spec = setup_wizard.PLATFORM_FIELDS[platform_key]
    values = setup_wizard.current_values()
    print(f"\n--- {spec['label']} setup ---")
    for i, line in enumerate(spec.get("setup_guide", []), 1):
        print(f"  {i}. {line}")

    collected: dict[str, str] = {}
    for fkey, fspec in spec["fields"].items():
        current = values.get(fkey, "")
        raw = _prompt_field(fkey, fspec, current, required=fkey in spec["gate_fields"])
        if raw:
            collected[fkey] = raw

    if not collected:
        print(f"\n{spec['label']}: nothing entered — skipped.")
        return
    backup, status = setup_wizard.apply_platform_fields(collected, actor="setup-wizard-cli")
    print(f"\nSaved.{' Backed up previous version as ' + backup.name + '.' if backup else ''}")
    if status[platform_key]["configured"]:
        print(f"{spec['label']}: configured.")
    else:
        print(f"{spec['label']}: saved what you entered, but it's not complete yet — re-run to finish it.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="print status and exit — no prompts")
    parser.add_argument("--all", action="store_true", help="reconfigure every field, even already-valid ones")
    args = parser.parse_args()

    status = setup_wizard.check_status()

    if args.check:
        _print_status(status)
        sys.exit(0 if status["ready"] else 1)

    print("Bot Server — setup")
    _print_status(status)

    if args.all:
        fields_to_ask = setup_wizard.FIELD_ORDER
    else:
        fields_to_ask = [k for k in setup_wizard.FIELD_ORDER if not status["fields"][k]["valid"]]

    try:
        if fields_to_ask:
            run_wizard(fields_to_ask)

        status = setup_wizard.check_status()
        if args.all or not any(p["configured"] for p in status["platforms"].values()):
            names = ", ".join(setup_wizard.PLATFORM_FIELDS.keys())
            print(f"\n--- Messaging platform ---\nPick at least one ({names}), comma-separated, or blank to skip for now.")
            choice = input("> ").strip().lower()
            for name in (c.strip() for c in choice.split(",")):
                if not name:
                    continue
                if name in setup_wizard.PLATFORM_FIELDS:
                    run_platform_wizard(name)
                else:
                    print(f"  unknown platform {name!r} — skipping")

        status = setup_wizard.check_status()
        _print_status(status)
        if status["ready"]:
            if platform.system() == "Windows":
                print("\nNext: .\\scripts\\run.ps1  (or, once built, launch BotServer.exe)")
            else:
                print("\nNext: ./scripts/run.sh  (or, once built, launch the bot-server binary)")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
