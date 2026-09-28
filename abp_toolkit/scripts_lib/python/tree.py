"""
name: tree
description: A folder as a tree with sizes, skipping dependency and build folders
params: [FOLDER] [--depth 3] [--sizes] [--all]
safety: read
"""
import argparse
import os
import sys

SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".mypy_cache", ".pytest_cache"}


def size_of(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def walk(path: str, prefix: str, depth: int, a) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return
    entries = [e for e in entries if a.all or (e.name not in SKIP and not e.name.startswith("."))]
    for i, e in enumerate(entries):
        last = i == len(entries) - 1
        size = ""
        if a.sizes:
            size = "  " + human(size_of(e.path) if e.is_dir() else e.stat().st_size)
        print(prefix + ("└── " if last else "├── ") + e.name + ("/" if e.is_dir() else "") + size)
        if e.is_dir() and depth > 1:
            walk(e.path, prefix + ("    " if last else "│   "), depth - 1, a)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Show a folder as a tree")
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--sizes", action="store_true")
    ap.add_argument("--all", action="store_true", help="include hidden and dependency folders")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(os.path.abspath(args.folder))
    walk(args.folder, "", args.depth, args)
