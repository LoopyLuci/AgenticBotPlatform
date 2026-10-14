"""python -m bot.context_vault status|tiers|recall|import|export|rebalance

Works on the configured vault whether or not `context_vault.enabled` is on (it is the owner's own command line);
nothing is created until a command runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _open():
    from bot.context_vault import embed, settings
    from bot.context_vault.store import Vault

    cfg = settings.load()
    return Vault(cfg, embedder=embed.from_settings(cfg))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot.context_vault", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="counters, per-tier items and bytes, hit rate")
    sub.add_parser("tiers", help="the configured tiers and where they live")
    r = sub.add_parser("recall", help="search the vault")
    r.add_argument("query")
    r.add_argument("--project", default=None)
    r.add_argument("--kind", action="append", dest="kinds")
    r.add_argument("-k", type=int, default=5)
    r.add_argument("--budget", type=int, default=0, help="token budget (0 = none)")
    i = sub.add_parser("import", help="load items from a JSON-lines file (redaction and dedup apply)")
    i.add_argument("file")
    e = sub.add_parser("export", help="write every item as JSON lines")
    e.add_argument("file", nargs="?", default="-")
    sub.add_parser("rebalance", help="bring every tier under its cap now")
    args = p.parse_args(argv)

    vault = _open()
    try:
        if args.cmd == "status":
            print(json.dumps(vault.stats(), indent=2))
        elif args.cmd == "tiers":
            print(json.dumps(vault.stats()["tiers"], indent=2))
        elif args.cmd == "recall":
            items = vault.recall(args.query, args.project, args.kinds, args.k, args.budget)
            vault.drain()
            print(json.dumps(items, indent=2))
        elif args.cmd == "import":
            rows = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
            print(json.dumps(vault.import_items(rows)))
            vault.drain()
        elif args.cmd == "export":
            text = "\n".join(json.dumps(it) for it in vault.export_items())
            if args.file == "-":
                print(text)
            else:
                Path(args.file).write_text(text + ("\n" if text else ""), encoding="utf-8")
        elif args.cmd == "rebalance":
            vault.drain()
            print(json.dumps(vault.rebalance()))
    finally:
        vault.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
