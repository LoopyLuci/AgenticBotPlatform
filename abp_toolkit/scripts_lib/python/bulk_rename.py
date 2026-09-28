"""
name: bulk_rename
description: Rename many files with a regular expression (shows the plan unless --apply; never overwrites)
params: FOLDER PATTERN REPLACEMENT [--apply] [--recursive]  (REPLACEMENT may use \\1 groups and {n} for a counter)
safety: changes
"""
import argparse
import os
import re
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename files with a regular expression")
    ap.add_argument("folder")
    ap.add_argument("pattern")
    ap.add_argument("replacement")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--recursive", action="store_true")
    a = ap.parse_args()
    rx = re.compile(a.pattern)
    plan = []
    walker = os.walk(a.folder) if a.recursive else [(a.folder, [], os.listdir(a.folder))]
    n = 0
    for root, _dirs, files in walker:
        for name in sorted(files):
            if not rx.search(name):
                continue
            n += 1
            new = rx.sub(a.replacement.replace("{n}", str(n)), name)
            if new != name:
                plan.append((os.path.join(root, name), os.path.join(root, new)))
    targets = [t for _s, t in plan]
    clashes = {t for t in targets if targets.count(t) > 1 or os.path.exists(t)}
    for src, dst in plan:
        mark = "CLASH " if dst in clashes else ""
        print(f"{mark}{os.path.basename(src)}  ->  {os.path.basename(dst)}")
    if clashes:
        print(f"{len(clashes)} name clash(es): nothing renamed.")
        return 1
    if a.apply:
        for src, dst in plan:
            os.rename(src, dst)
        print(f"Renamed {len(plan)} file(s).")
    else:
        print(f"{len(plan)} file(s) would be renamed. Add --apply to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
