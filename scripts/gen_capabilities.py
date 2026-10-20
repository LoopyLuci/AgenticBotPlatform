"""Regenerate ABP's capability index: docs/capabilities.json and docs/capabilities.md (bot/capabilities).

    python scripts/gen_capabilities.py            write both
    python scripts/gen_capabilities.py --check    exit 1 if either is out of date

Run it after adding a page, an action, an API route, a module or a settings section (tests/test_capabilities.py fails
until you do)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    from bot import capabilities

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        doc = capabilities.generate(ROOT)
        want_json = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        stale = [p for p, want in (("docs/capabilities.json", want_json), ("docs/capabilities.md", capabilities.markdown(doc)))
                 if not (ROOT / p).is_file() or (ROOT / p).read_text(encoding="utf-8") != want]
        if stale:
            print("out of date: " + ", ".join(stale) + " - run python scripts/gen_capabilities.py", file=sys.stderr)
            return 1
        return 0
    for p in capabilities.write(ROOT):
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
