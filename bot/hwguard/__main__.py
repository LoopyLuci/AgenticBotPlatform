"""Hardware guard for tools outside ABP (the pipeline, the render harness, scripts).

    python -m bot.hwguard status                                  the verdict now, as JSON
    python -m bot.hwguard workers --kind pytest --want 6          prints the allowed count (0 = wait)
    python -m bot.hwguard acquire --kind render --pid N [--label L] [--wait S]   exit 0 granted, 3 refused
    python -m bot.hwguard release --pid N [--force]
    python -m bot.hwguard watch --kind render --pid N [--release-pid M]
        suspends process N while the verdict is pause and resumes it after; returns when N exits, then frees M's slot
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from bot.hwguard import governor, slot


def watch(kind: str, pid: int, release_pid: int = 0, every_s: float = 10.0) -> int:
    import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return 0
    suspended = False
    try:
        while proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
            cfg = governor.settings()
            v = governor.verdict(max_age_s=30)
            stop = cfg.get("enabled", True) and cfg["govern"].get(kind, True) and v["level"] == governor.PAUSE
            if stop and not suspended:
                proc.suspend()
                suspended = True
                print(f"hwguard: paused {kind} pid {pid}: " + "; ".join(v["reasons"]), file=sys.stderr, flush=True)
            elif suspended and not stop:
                proc.resume()
                suspended = False
                print(f"hwguard: resumed {kind} pid {pid}", file=sys.stderr, flush=True)
            time.sleep(every_s)
    except psutil.Error:
        pass
    finally:
        if suspended:
            try:
                proc.resume()
            except psutil.Error:
                pass
        if release_pid:
            slot.release(release_pid)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.hwguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    w = sub.add_parser("workers")
    w.add_argument("--kind", required=True, choices=governor.KINDS)
    w.add_argument("--want", type=int, required=True)
    a = sub.add_parser("acquire")
    a.add_argument("--kind", required=True, choices=governor.KINDS)
    a.add_argument("--pid", type=int, required=True)
    a.add_argument("--label", default="")
    a.add_argument("--wait", type=float, default=0.0)
    r = sub.add_parser("release")
    r.add_argument("--pid", type=int, default=0)
    r.add_argument("--force", action="store_true")
    wa = sub.add_parser("watch")
    wa.add_argument("--kind", required=True, choices=governor.KINDS)
    wa.add_argument("--pid", type=int, required=True)
    wa.add_argument("--release-pid", type=int, default=0)
    ns = ap.parse_args(argv)
    if ns.cmd == "status":
        print(json.dumps(governor.verdict(), indent=1))
        return 0
    if ns.cmd == "workers":
        print(governor.cap(ns.kind, ns.want))
        return 0
    if ns.cmd == "acquire":
        res = slot.acquire(ns.kind, ns.label, pid=ns.pid, wait_s=ns.wait)
        print(json.dumps(res))
        return 0 if res["granted"] else 3
    if ns.cmd == "release":
        return 0 if slot.release(ns.pid or None, force=ns.force) else 1
    return watch(ns.kind, ns.pid, ns.release_pid)


if __name__ == "__main__":
    sys.exit(main())
