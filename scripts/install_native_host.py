"""Registers ABP's native-messaging host (bot/native_host.py) with installed Chromium browsers and Firefox, so the ABP
Bridge extension can ask a small helper to check on or start ABP when its usual loopback WebSocket cannot be reached
(docs/browser-extension/DESIGN.md section 3.2). Safe to re-run; `--uninstall` removes what it registered.

    python scripts/install_native_host.py --extension-id <chrome/edge id> [--firefox-id <id@example.com>] [--launcher <path>]
    python scripts/install_native_host.py --uninstall

This changes browser and (on Windows) registry configuration, so it is meant to be run BY the person, not on their
behalf - it is never invoked automatically by anything else in this repo.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
HOST_NAME = "com.abp.bridge"


def default_launcher() -> Optional[Path]:
    """Best guess at what should be started to bring ABP up: the built desktop app if one exists next to this
    checkout, else `python -m bot.main` run from the project root."""
    exe = ROOT / "desktop-app" / "src-tauri" / "target" / "release" / "agentic-bot-platform.exe"
    if exe.exists():
        return exe
    return ROOT / "bot" / "main.py"


def build_manifest(*, path: Path, chrome_ids: list[str], firefox_ids: list[str]) -> dict:
    manifest: dict = {"name": HOST_NAME, "description": "Lets the ABP Bridge browser extension check on or start the ABP desktop app.",
                       "path": str(path), "type": "stdio"}
    if firefox_ids:
        manifest["allowed_extensions"] = firefox_ids
    else:
        manifest["allowed_origins"] = [f"chrome-extension://{i}/" for i in chrome_ids]
    return manifest


def write_wrapper(python_exe: str) -> Path:
    """A tiny native-host wrapper `path` can point at directly (native messaging requires a real executable, not a
    Python file). Windows: a .bat that calls this venv's python; POSIX: a shebang'd shell script."""
    out_dir = ROOT / "native_host"
    out_dir.mkdir(exist_ok=True)
    if sys.platform == "win32":
        wrapper = out_dir / "abp_native_host.bat"
        wrapper.write_text(f'@echo off\r\n"{python_exe}" -m bot.native_host\r\n', encoding="utf-8")
    else:
        wrapper = out_dir / "abp_native_host.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{python_exe}" -m bot.native_host\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def manifest_paths(browser: str) -> list[Path]:
    """Every place this OS/browser combination expects a native-messaging manifest for `browser` (some browsers keep
    more than one profile-independent location; only user-level locations are used, never system-wide)."""
    home = Path.home()
    if sys.platform == "win32":
        # Windows manifests can live anywhere; only the registry value naming their path actually matters there
        # (see register_windows below). Still write a canonical copy so the registry has a stable target.
        return [ROOT / "native_host" / f"{HOST_NAME}.{browser}.json"]
    if sys.platform == "darwin":
        dirs = {"chrome": home / "Library/Application Support/Google/Chrome/NativeMessagingHosts",
                "edge": home / "Library/Application Support/Microsoft Edge/NativeMessagingHosts",
                "firefox": home / "Library/Application Support/Mozilla/NativeMessagingHosts"}
    else:
        dirs = {"chrome": home / ".config/google-chrome/NativeMessagingHosts",
                "edge": home / ".config/microsoft-edge/NativeMessagingHosts",
                "firefox": home / ".mozilla/native-messaging-hosts"}
    d = dirs.get(browser)
    return [d / f"{HOST_NAME}.json"] if d else []


def register_windows(browser: str, manifest_path: Path) -> None:  # pragma: no cover - exercised only on a real Windows install
    import winreg

    key_path = {"chrome": r"Software\Google\Chrome\NativeMessagingHosts\%s" % HOST_NAME,
                "edge": r"Software\Microsoft\Edge\NativeMessagingHosts\%s" % HOST_NAME,
                "firefox": r"Software\Mozilla\NativeMessagingHosts\%s" % HOST_NAME}[browser]
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValue(key, "", winreg.REG_SZ, str(manifest_path))


def unregister_windows(browser: str) -> None:  # pragma: no cover - exercised only on a real Windows install
    import winreg

    key_path = {"chrome": r"Software\Google\Chrome\NativeMessagingHosts\%s" % HOST_NAME,
                "edge": r"Software\Microsoft\Edge\NativeMessagingHosts\%s" % HOST_NAME,
                "firefox": r"Software\Mozilla\NativeMessagingHosts\%s" % HOST_NAME}[browser]
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except FileNotFoundError:
        pass


def install(*, chrome_ids: list[str], firefox_ids: list[str], launcher: Path, browsers: list[str]) -> list[Path]:
    wrapper = write_wrapper(sys.executable)
    (ROOT / "native_host_install.json").write_text(json.dumps({"launcher": str(launcher)}, indent=2), encoding="utf-8")
    written: list[Path] = []
    for browser in browsers:
        if browser == "firefox" and not firefox_ids:
            continue
        if browser != "firefox" and not chrome_ids:
            continue
        manifest = build_manifest(path=wrapper, chrome_ids=chrome_ids, firefox_ids=firefox_ids if browser == "firefox" else [])
        for p in manifest_paths(browser):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            written.append(p)
            if sys.platform == "win32":
                register_windows(browser, p)
    return written


def uninstall(browsers: list[str]) -> None:
    for browser in browsers:
        for p in manifest_paths(browser):
            p.unlink(missing_ok=True)
        if sys.platform == "win32":
            unregister_windows(browser)
    shutil.rmtree(ROOT / "native_host", ignore_errors=True)
    (ROOT / "native_host_install.json").unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extension-id", action="append", default=[], help="a Chrome/Edge extension id (repeatable; the dev build's id is in browser-extension/manifest/dev-key.json)")
    ap.add_argument("--firefox-id", action="append", default=[], help="a Firefox extension id, e.g. abp-bridge@yourdomain (repeatable)")
    ap.add_argument("--launcher", type=Path, default=None, help="what to start when the browser asks ABP to launch (default: the built desktop app, or bot/main.py)")
    ap.add_argument("--browsers", nargs="+", choices=["chrome", "edge", "firefox"], default=["chrome", "edge", "firefox"])
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)

    if args.uninstall:
        uninstall(args.browsers)
        print("Removed the ABP native-messaging host registration.")
        return 0

    if not args.extension_id and not args.firefox_id:
        ap.error("--extension-id (or --firefox-id) is required - see browser-extension/manifest/dev-key.json for a development build's id")
    launcher = args.launcher or default_launcher()
    if launcher is None or not launcher.exists():
        ap.error(f"--launcher does not exist: {launcher}")
    written = install(chrome_ids=args.extension_id, firefox_ids=args.firefox_id, launcher=launcher, browsers=args.browsers)
    for p in written:
        print(f"wrote {p}")
    print(f"Native host registered. ABP starts from: {launcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
