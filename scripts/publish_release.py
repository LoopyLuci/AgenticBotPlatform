#!/usr/bin/env python
"""Publishes one BotServer release — the single, repeatable process for
"a new version, out on GitHub, with the right file attached for each app's
own auto-updater to actually find."

Why this exists: every release from v0.2.2 through v0.7.10 was published by
hand (ad-hoc `gh release create`/`gh release upload` calls, not a checked-in
script). That let two real bugs slip in silently:

1. Desktop's Cargo.toml/tauri.conf.json version was never bumped past 0.4.0
   across 15+ releases, while the shared git-tag sequence climbed to 0.7.10 —
   so the desktop app's own updater (desktop-app/src-tauri/src/updater.rs)
   correctly saw "0.7.10 is newer than my 0.4.0" but could never actually
   install it.
2. The desktop asset uploaded to those releases was the bare
   `bot-server.exe` (the raw, unpacked binary) instead of the real NSIS
   installer (`bundle/nsis/BotServer_<ver>_x64-setup.exe`) that
   `cargo tauri build` actually produces. updater.rs specifically looks for
   an asset whose name ends in "-setup.exe" (the only thing `install_update()`
   can silently run with NSIS's `/S` flag) — a bare .exe doesn't match, so
   `download_url` came back None and the Updates panel had nothing to
   download even once the version numbers agreed.

Android's own side of this (android-app/app/build.gradle.kts's versionName)
was already being bumped correctly release-to-release, and its updater
(GitHubUpdateRepository.kt) matches by ".apk" asset suffix, which the manual
process did upload correctly each time — so Android was never broken by
this, only Desktop was. This script keeps Android's already-correct pattern
and fixes Desktop's to match it, in one shared release, so both platforms'
version numbers and assets are always consistent with each other and with
what actually shipped.

Usage:
    python scripts/publish_release.py <version> "<release title>" ["<notes body>"]

Example:
    python scripts/publish_release.py 0.7.11 "Fix desktop auto-update" \\
        "The desktop app's Updates panel can now actually download and \\
install a new version — previous releases uploaded the wrong file."

What it does, in order (aborts loudly, before touching git/GitHub, if any
build step fails or produces the wrong file):
  1. Refuses to run with uncommitted changes in the tree (safety — a
     release commit should contain exactly the version bump, nothing else
     accidentally swept in).
  2. Bumps desktop-app/src-tauri/Cargo.toml's `version` and
     desktop-app/src-tauri/tauri.conf.json's `"version"` to match.
  3. Bumps android-app/app/build.gradle.kts's `versionName` to match and
     increments `versionCode` by 1.
  4. Commits the bump (`Release v<version>`).
  5. Builds the desktop app (`cargo tauri build`) and verifies the real NSIS
     installer exists and its filename ends in "-setup.exe" — the exact
     property updater.rs's own asset search depends on.
  6. Builds the Android debug APK (`gradlew assembleDebug`) and verifies it
     exists.
  7. Tags `v<version>` and pushes the commit + tag.
  8. `gh release create` with both real build artifacts attached — nothing
     else, so there's no ambiguity about which file is which app's.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_DIR = ROOT / "desktop-app" / "src-tauri"
CARGO_TOML = DESKTOP_DIR / "Cargo.toml"
TAURI_CONF = DESKTOP_DIR / "tauri.conf.json"
ANDROID_GRADLE = ROOT / "android-app" / "app" / "build.gradle.kts"
ANDROID_DIR = ROOT / "android-app"
IS_WINDOWS = sys.platform.startswith("win")


def die(msg: str) -> None:
    print(f"\n[FAILED] {msg}\n", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}  (cwd={cwd})")
    result = subprocess.run(cmd, cwd=cwd, text=True)
    if check and result.returncode != 0:
        die(f"command failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def validate_version(v: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        die(f"version must be X.Y.Z (e.g. 0.7.11), got {v!r}")


def ensure_clean_tree() -> None:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    dirty = [line for line in result.stdout.splitlines() if not line.startswith("??")]
    if dirty:
        die(
            "working tree has uncommitted changes — commit or stash them first "
            "(a release commit should contain only the version bump):\n" + "\n".join(dirty)
        )


def bump_cargo_toml(version: str) -> None:
    text = CARGO_TOML.read_text(encoding="utf-8")
    new_text, n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{version}"', text, count=1)
    if n != 1:
        die(f"couldn't find a `version = \"...\"` line in {CARGO_TOML}")
    CARGO_TOML.write_text(new_text, encoding="utf-8")
    print(f"bumped {CARGO_TOML.relative_to(ROOT)} -> {version}")


def bump_tauri_conf(version: str) -> None:
    data = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    data["version"] = version
    TAURI_CONF.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"bumped {TAURI_CONF.relative_to(ROOT)} -> {version}")


def bump_android_gradle(version: str) -> None:
    text = ANDROID_GRADLE.read_text(encoding="utf-8")
    text, n1 = re.subn(r'(?m)^(\s*versionName = )"[^"]*"', rf'\1"{version}"', text, count=1)
    if n1 != 1:
        die(f"couldn't find a `versionName = \"...\"` line in {ANDROID_GRADLE}")

    m = re.search(r"(?m)^\s*versionCode = (\d+)", text)
    if not m:
        die(f"couldn't find a `versionCode = N` line in {ANDROID_GRADLE}")
    new_code = int(m.group(1)) + 1
    text = re.sub(r"(?m)^(\s*versionCode = )\d+", rf"\g<1>{new_code}", text, count=1)

    ANDROID_GRADLE.write_text(text, encoding="utf-8")
    print(f"bumped {ANDROID_GRADLE.relative_to(ROOT)} -> versionName {version}, versionCode {new_code}")


def build_desktop(version: str) -> Path:
    run(["cargo", "tauri", "build"], cwd=DESKTOP_DIR)
    nsis_dir = DESKTOP_DIR / "target" / "release" / "bundle" / "nsis"
    candidates = sorted(nsis_dir.glob("*-setup.exe")) if nsis_dir.exists() else []
    if not candidates:
        die(
            f"no *-setup.exe found in {nsis_dir} after `cargo tauri build` — "
            "this is exactly the file updater.rs's check_for_update() looks for; "
            "publishing without it would reproduce the original bug."
        )
    installer = candidates[-1]
    if version not in installer.name:
        print(f"WARNING: installer name {installer.name!r} doesn't contain {version!r} — check tauri.conf.json's version took effect.")
    print(f"desktop installer: {installer}")
    return installer


def build_android() -> Path:
    gradlew = ANDROID_DIR / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    if not gradlew.exists():
        die(f"{gradlew} not found")
    run([str(gradlew), "assembleDebug", "--console=plain"], cwd=ANDROID_DIR)
    apk = ANDROID_DIR / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    if not apk.is_file():
        die(f"expected APK not found at {apk} after `gradlew assembleDebug`")
    print(f"android APK: {apk}")
    return apk


def main() -> None:
    if len(sys.argv) < 3:
        die("usage: python scripts/publish_release.py <version> \"<title>\" [\"<notes>\"]")
    version = sys.argv[1]
    title = sys.argv[2]
    notes = sys.argv[3] if len(sys.argv) > 3 else title
    tag = f"v{version}"

    validate_version(version)
    ensure_clean_tree()

    existing_tags = subprocess.run(["git", "tag", "-l", tag], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if existing_tags:
        die(f"tag {tag} already exists — pick a new version")

    print(f"\n=== Publishing BotServer {tag} ===\n")

    bump_cargo_toml(version)
    bump_tauri_conf(version)
    bump_android_gradle(version)

    run(["git", "add", str(CARGO_TOML), str(TAURI_CONF), str(ANDROID_GRADLE)])
    run(["git", "commit", "-m", f"Release {tag}"])

    installer = build_desktop(version)
    apk = build_android()

    run(["git", "tag", tag])
    run(["git", "push"])
    run(["git", "push", "origin", tag])

    run([
        "gh", "release", "create", tag,
        "--title", title,
        "--notes", notes,
        str(installer),
        str(apk),
    ])

    print(f"\n=== {tag} published ===")
    print(f"  desktop: {installer.name}")
    print(f"  android: {apk.name}")
    print("Verify: the desktop app's Updates panel should now offer a working download,")
    print("and Android's in-app update check should offer this release's APK.")


if __name__ == "__main__":
    main()
