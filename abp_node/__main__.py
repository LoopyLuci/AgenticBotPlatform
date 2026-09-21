"""A reference node: a stand-in phone for developing and testing the node protocol (roadmap P7).

    python -m abp_node --server http://127.0.0.1:8765 --key <a paired device's key> --name "Test phone" \\
                       --capabilities location.get,notify.show,clipboard.read

It registers, long-polls for commands and answers them with canned data (a fixed location, a made-up clipboard, a printed
notification). It is what an app has to do, in about sixty lines, and how the server half was tested. **It is not a real phone**:
no camera, screen or GPS is read. The Android app does not implement this protocol yet.

`--ask` makes it behave like a phone whose capabilities are set to "device": it asks on the terminal before answering.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from typing import Callable, Optional

import httpx

# A 1x1 transparent PNG, so camera.snap and screen.capture have something to return.
_PIXEL = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489000000"
    "0d49444154789c6360000002000001e221bc330000000049454e44ae426082")).decode()

CANNED: dict[str, Callable[[dict], dict]] = {
    "location.get": lambda args: {"lat": 40.7128, "lon": -74.0060, "accuracy_m": 25, "provider": "reference-node"},
    "clipboard.read": lambda args: {"text": "text on the reference node's clipboard"},
    "notify.show": lambda args: {"shown": True, "text": str(args.get("text", ""))[:200]},
    "camera.snap": lambda args: {"image_b64": _PIXEL, "mime": "image/png"},
    "screen.capture": lambda args: {"image_b64": _PIXEL, "mime": "image/png"},
}


class ReferenceNode:
    def __init__(self, base_url: str, key: str, *, name: str = "Reference node", capabilities: Optional[list[str]] = None,
                 http: Optional[httpx.Client] = None, ask: Optional[Callable[[str, dict], bool]] = None):
        self.http = http or httpx.Client(base_url=base_url, timeout=90)
        self.headers = {"X-Dashboard-Token": key}
        self.name = name
        self.capabilities = capabilities or list(CANNED)
        self.ask = ask                       # called for a command whose consent is "device": True = the user allows it

    def register(self) -> list[str]:
        r = self.http.post("/api/nodes/register", json={"name": self.name, "capabilities": self.capabilities}, headers=self.headers)
        r.raise_for_status()
        return r.json()["capabilities"]

    def answer(self, command: dict) -> dict:
        cap, args = command["capability"], command.get("args") or {}
        if args.get("consent") == "device" and self.ask is not None and not self.ask(cap, args):
            return {"id": command["id"], "ok": False, "error": "the user declined"}
        handler = CANNED.get(cap)
        if handler is None or cap not in self.capabilities:
            return {"id": command["id"], "ok": False, "error": f"this node does not do {cap}"}
        return {"id": command["id"], "ok": True, "data": handler(args)}

    def run_once(self, wait: float = 25.0) -> int:
        """One poll: answer whatever is waiting. Returns how many commands were handled."""
        r = self.http.get("/api/nodes/poll", params={"wait": wait}, headers=self.headers)
        r.raise_for_status()
        commands = r.json()["commands"]
        for command in commands:
            self.http.post("/api/nodes/result", json=self.answer(command), headers=self.headers).raise_for_status()
        return len(commands)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_node", description=__doc__.splitlines()[0])
    ap.add_argument("--server", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--name", default="Reference node")
    ap.add_argument("--capabilities", default=",".join(CANNED))
    ap.add_argument("--ask", action="store_true", help="ask on the terminal before answering a command set to 'device' consent")
    args = ap.parse_args(argv)

    def ask(cap: str, a: dict) -> bool:
        return input(f"The agent wants: {cap} {a}. Allow? [y/N] ").strip().lower().startswith("y")

    node = ReferenceNode(args.server, args.key, name=args.name, capabilities=[c for c in args.capabilities.split(",") if c],
                         ask=ask if args.ask else None)
    print("registered with:", ", ".join(node.register()))
    while True:
        try:
            n = node.run_once()
            if n:
                print(f"answered {n} command(s)")
        except httpx.HTTPError as exc:
            print("connection problem:", exc, file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
