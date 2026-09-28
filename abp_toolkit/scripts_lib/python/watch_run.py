"""
name: watch_run
description: Watch a folder and run a command whenever a file in it changes (tests on save, rebuilds), without extra packages
params: FOLDER COMMAND... [--ext .py,.js] [--interval 1.0]
safety: executes
"""
import argparse
import os
import subprocess
import sys
import time

SKIP = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", "target"}


def snapshot(folder: str, exts: tuple) -> dict:
    out = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if not exts or f.endswith(exts):
                p = os.path.join(root, f)
                try:
                    out[p] = os.stat(p).st_mtime_ns
                except OSError:
                    pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a command when files change")
    ap.add_argument("folder")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    ap.add_argument("--ext", default="")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    if not a.command:
        ap.error("give the command to run")
    exts = tuple(e.strip() for e in a.ext.split(",") if e.strip())
    before = snapshot(a.folder, exts)
    print(f"Watching {a.folder} (Ctrl+C stops). Running: {' '.join(a.command)}")
    subprocess.call(a.command)
    try:
        while True:
            time.sleep(a.interval)
            now = snapshot(a.folder, exts)
            changed = [p for p in now if before.get(p) != now[p]] + [p for p in before if p not in now]
            if changed:
                print(f"\n--- {len(changed)} change(s): {', '.join(os.path.relpath(p, a.folder) for p in changed[:5])}")
                subprocess.call(a.command)
                before = now
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
