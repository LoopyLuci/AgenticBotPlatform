"""
name: find_duplicates
description: Find duplicate files under a folder by content (size first, then SHA-256); optionally delete all but the first copy
params: PATH [--delete] [--min-size BYTES]
safety: changes
"""
import argparse
import hashlib
import os
import sys
from collections import defaultdict


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Find duplicate files by content")
    ap.add_argument("path")
    ap.add_argument("--delete", action="store_true", help="delete every copy but the first (by path)")
    ap.add_argument("--min-size", type=int, default=1)
    a = ap.parse_args()
    by_size = defaultdict(list)
    for root, dirs, files in os.walk(a.path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for name in files:
            p = os.path.join(root, name)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size >= a.min_size:
                by_size[size].append(p)
    groups = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) > 1:
            for p in paths:
                try:
                    groups[(size, sha256(p))].append(p)
                except OSError:
                    pass
    wasted = 0
    for (size, digest), paths in sorted(groups.items(), key=lambda kv: -kv[0][0]):
        if len(paths) < 2:
            continue
        paths.sort()
        wasted += size * (len(paths) - 1)
        print(f"{size:>12,} bytes  {digest[:12]}")
        for i, p in enumerate(paths):
            print(("   keep  " if i == 0 else "   dup   ") + p)
            if i and a.delete:
                os.remove(p)
    print(f"{'Freed' if a.delete else 'Duplicates use'} {wasted / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
