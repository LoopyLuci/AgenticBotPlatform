"""
name: serve_folder
description: Serve a folder over HTTP on this computer only (127.0.0.1) for previewing a website or sharing files locally
params: [FOLDER] [--port 8000] [--lan]  (--lan listens on every network address)
safety: network
"""
import argparse
import functools
import http.server
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve a folder over HTTP")
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="listen on every network address, not just this computer")
    a = ap.parse_args()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=a.folder)
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    with http.server.ThreadingHTTPServer((host, a.port), handler) as httpd:
        print(f"Serving {a.folder} at http://{'127.0.0.1' if not a.lan else host}:{a.port}  (Ctrl+C stops)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
