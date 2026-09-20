#!/usr/bin/env python
"""Cut and publish an AgenticBotPlatform release — desktop installer + Android
APK — in one command, so version numbers and assets are always consistent with
each other and with what actually shipped.

Usage:
    python scripts/publish_release.py <version> "<release title>" ["<notes body>"]
        [--resume] [--skip-gate] [--dry-run]

Example:
    python scripts/publish_release.py 0.7.11 "Fix desktop auto-update" \\
        "The desktop app's Updates panel can now actually download and install."

The release is a TRANSACTION. Everything that can go wrong is checked before
anything is modified, transient failures heal and retry, and what cannot be
finished is rolled back — see scripts/release_guard.py.

  Before touching anything (--dry-run stops here):
    * one release/pipeline at a time (.pipeline.lock)
    * git: on main, not behind origin, no rebase/merge in progress, no stale
      index.lock (cleared if no git is running), tracked files committed
    * no source files hidden by .gitignore (an unanchored `data/` once hid the
      Android `data` package)
    * version is X.Y.Z, newer than every tag, tag unused locally AND on origin
    * Cargo.lock's own version synced to the release (was a manual pre-step)
    * cargo / cargo-tauri / gradle / gh installed, gh logged in, GitHub
      reachable, disk space, and the update-signing key matches the public key
      embedded in the app
    * lock healing: stop processes still running out of the built app / staged
      venv (a leftover backend pins _rust.pyd and fails the build) and prove
      the files are free

  Then, journalled step by step (.release_journal.json):
    bumped -> committed -> gated -> built_desktop -> smoke -> built_android
    -> tagged -> pushed -> tag_pushed -> released -> verified
    * gated: the full local pipeline (pytest, Rust, Android) runs ONCE on the
      release commit, before any tag or push. The pre-push hook then sees
      ABP_PIPELINE_PASSED_SHA and does not re-run it — or rebuild the app over
      the installer that is about to be signed (the v0.7.24 signature bug).
    * smoke: the staged installer bundle is booted against a throwaway state
      dir and must answer /healthz before it can be tagged.
    * before each irreversible step HEAD and the tracked tree must be exactly
      as the release left them (an edit mid-release aborts instead of racing).
    * network steps (push, tag push, gh release, verification) retry with
      backoff; a locked file heals and retries; a compile/test failure does not.
    * signing happens immediately before upload and the published asset digests
      and signature are read back and verified.

  On failure: steps that never left this machine (the release commit, the local
  tag, uncommitted bumps) are rolled back automatically. If it failed after the
  push, nothing pushed is rewritten — finish with the same command plus --resume.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import release_guard as guard
import update_signing

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_DIR = ROOT / "desktop-app" / "src-tauri"
CARGO_TOML = DESKTOP_DIR / "Cargo.toml"
TAURI_CONF = DESKTOP_DIR / "tauri.conf.json"
ANDROID_GRADLE = ROOT / "android-app" / "app" / "build.gradle.kts"
ANDROID_DIR = ROOT / "android-app"
JOURNAL_PATH = ROOT / ".release_journal.json"
LOCK_PATH = ROOT / ".pipeline.lock"
IS_WINDOWS = sys.platform.startswith("win")

GATE_TIMEOUT = 60 * 60
BUILD_TIMEOUT = 45 * 60


class ReleaseError(Exception):
    """A release step failed; main() rolls back what it can and reports."""


def die(msg: str) -> None:
    print(f"\n[FAILED] {msg}\n", file=sys.stderr)
    sys.exit(1)


class _Tee:
    """Everything printed also lands in logs/release/<timestamp>.log."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "w", encoding="utf-8", errors="replace")
        self._out = sys.__stdout__

    def write(self, s: str) -> int:
        self._out.write(s)
        self._f.write(s)
        self._f.flush()
        return len(s)

    def flush(self) -> None:
        self._out.flush()
        self._f.flush()


def step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def must(cmd: list[str], *, cwd: Path | None = None, timeout: float | None = None, what: str = "") -> guard.CmdResult:
    """Run once, stream output, raise ReleaseError on failure."""
    cwd = cwd or ROOT
    print(f"+ {' '.join(cmd)}  (cwd={cwd})")
    res = guard.run_cmd(cmd, cwd=cwd, timeout=timeout)
    if not res.ok:
        raise ReleaseError(f"{what or ' '.join(cmd[:3])} failed (exit {res.rc})")
    return res


def retrying(cmd, *, cwd: Path | None = None, attempts: int = 3, base_delay: float = 3.0,
             timeout: float | None = None, heal=None, what: str = "") -> guard.CmdResult:
    cwd = cwd or ROOT
    shown = cmd() if callable(cmd) else cmd
    print(f"+ {' '.join(shown)}  (cwd={cwd})")
    res = guard.run_with_retry(cmd, cwd=cwd, attempts=attempts, base_delay=base_delay,
                               timeout=timeout, heal=heal)
    if not res.ok:
        raise ReleaseError(f"{what or ' '.join(shown[:3])} failed (exit {res.rc}, "
                           f"{guard.classify_failure(res.output)}) after {attempts} attempt(s)")
    return res


def _read_text(path: Path) -> tuple[str, str]:
    """(text normalised to "\n", the file's own line ending). Text-mode I/O
    would silently rewrite every line ending to the platform's - CRLF on
    Windows - and .gitattributes forces some of these files to LF."""
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _write_text(path: Path, text: str, eol: str) -> None:
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))


def validate_version(v: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        die(f"version must be X.Y.Z (e.g. 0.7.11), got {v!r}")


def bump_cargo_toml(version: str) -> None:
    text, eol = _read_text(CARGO_TOML)
    new_text, n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{version}"', text, count=1)
    if n != 1:
        raise ReleaseError(f"couldn't find a `version = \"...\"` line in {CARGO_TOML}")
    _write_text(CARGO_TOML, new_text, eol)
    print(f"bumped {CARGO_TOML.relative_to(ROOT)} -> {version}")


def bump_tauri_conf(version: str) -> None:
    text, eol = _read_text(TAURI_CONF)
    data = json.loads(text)
    data["version"] = version
    _write_text(TAURI_CONF, json.dumps(data, indent=2) + "\n", eol)
    print(f"bumped {TAURI_CONF.relative_to(ROOT)} -> {version}")


def bump_android_gradle(version: str) -> None:
    text, eol = _read_text(ANDROID_GRADLE)
    text, n1 = re.subn(r'(?m)^(\s*versionName = )"[^"]*"', rf'\1"{version}"', text, count=1)
    if n1 != 1:
        raise ReleaseError(f"couldn't find a `versionName = \"...\"` line in {ANDROID_GRADLE}")
    m = re.search(r"(?m)^\s*versionCode = (\d+)", text)
    if not m:
        raise ReleaseError(f"couldn't find a `versionCode = N` line in {ANDROID_GRADLE}")
    new_code = int(m.group(1)) + 1
    text = re.sub(r"(?m)^(\s*versionCode = )\d+", rf"\g<1>{new_code}", text, count=1)
    _write_text(ANDROID_GRADLE, text, eol)
    print(f"bumped {ANDROID_GRADLE.relative_to(ROOT)} -> versionName {version}, versionCode {new_code}")


def assert_stable(expected_head: str) -> None:
    """HEAD and the tracked tree must be exactly as the release left them. A
    concurrent edit or commit mid-release (which once caused spurious pipeline
    failures) aborts here instead of being tagged and pushed."""
    head = guard.head_sha(ROOT)
    if head != expected_head:
        raise ReleaseError(f"HEAD moved during the release ({head[:8]} != {expected_head[:8]}) — "
                           "something committed while it was running")
    tree = guard.check_clean_tree(ROOT)
    if tree.blocking:
        raise ReleaseError(f"tracked files changed during the release: {tree.detail}")


def build_desktop(version: str) -> Path:
    retrying(["cargo", "tauri", "build"], cwd=DESKTOP_DIR, attempts=3, base_delay=5, timeout=BUILD_TIMEOUT,
             heal=lambda: guard.heal_locks(ROOT), what="cargo tauri build")
    nsis_dir = DESKTOP_DIR / "target" / "release" / "bundle" / "nsis"
    candidates = sorted(nsis_dir.glob("*-setup.exe")) if nsis_dir.exists() else []
    if not candidates:
        raise ReleaseError(
            f"no *-setup.exe found in {nsis_dir} after `cargo tauri build` — this is exactly the "
            "file updater.rs's check_for_update() looks for; publishing without it would reproduce the original bug."
        )
    # EXACTLY this version's installer. The folder keeps every earlier build,
    # and taking the alphabetically last one shipped a stale 0.7.99 installer
    # ("0.7.99" sorts after "0.7.28") whose app then reports version 0.7.99 and
    # never updates again.
    matching = [c for c in candidates if f"_{version}_" in c.name]
    if len(matching) != 1:
        raise ReleaseError(
            f"expected exactly one installer for {version} in {nsis_dir}, found {[c.name for c in matching]} "
            f"(the folder holds: {[c.name for c in candidates][-4:]}) — check tauri.conf.json's version took effect."
        )
    installer = matching[0]
    embedded = guard.installer_product_version(installer)
    if embedded is not None and embedded != version:
        raise ReleaseError(f"{installer.name} identifies itself as version {embedded}, not {version} — "
                           "refusing to ship an installer whose app would report the wrong version")
    print(f"desktop installer: {installer} (embedded version {embedded or 'unchecked'})")
    return installer


def current_installer(previous: Path) -> Path:
    """The installer as it is on disk NOW."""
    if not previous.is_file():
        raise ReleaseError(f"installer {previous} disappeared — refusing to sign/upload a guess")
    return previous


def build_android() -> Path:
    gradlew = ANDROID_DIR / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    if not gradlew.exists():
        raise ReleaseError(f"{gradlew} not found")
    retrying([str(gradlew), "assembleDebug", "--console=plain"], cwd=ANDROID_DIR, attempts=2,
             base_delay=5, timeout=BUILD_TIMEOUT, what="gradlew assembleDebug")
    apk = ANDROID_DIR / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    if not apk.is_file():
        raise ReleaseError(f"expected APK not found at {apk} after `gradlew assembleDebug`")
    print(f"android APK: {apk}")
    return apk


def release_exists(tag: str) -> bool:
    return guard._run(["gh", "release", "view", tag], cwd=ROOT, timeout=60).ok


def verify_published_assets(tag: str, installer: Path, sig: Path, *, attempts: int = 5, delay: float = 4.0,
                            sleep=time.sleep) -> None:
    """After upload: the installer/.sig GitHub recorded must be byte-identical
    to what was signed, and the signature must verify against the key embedded
    in the app. GitHub computes digests a moment after upload, so a missing
    digest is retried; a WRONG one fails at once."""
    published: dict[str, str] = {}
    for attempt in range(1, attempts + 1):
        result = guard._run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/releases/tags/{tag}", "--jq",
             '.assets[] | "\\(.name) \\(.digest)"'], cwd=ROOT, timeout=60)
        if result.ok:
            published = dict(line.rsplit(" ", 1) for line in result.output.splitlines() if " " in line)
            if all(published.get(p.name, "null") not in ("", "null") for p in (installer, sig)):
                break
        if attempt < attempts:
            sleep(delay)
    else:
        raise ReleaseError(f"published {tag} but couldn't read its asset digests back: {result.output.strip()[:200]}")
    # A release must carry exactly ONE installer, and it must be ours. Comparing
    # only our own files to themselves cannot notice a wrong or extra installer.
    stray = sorted(n for n in published if n.endswith("-setup.exe") and n != installer.name)
    if stray:
        raise ReleaseError(f"{tag} IS PUBLISHED with an unexpected installer attached: {stray} "
                           f"(expected only {installer.name}). Remove it: gh release delete-asset {tag} <name> --yes")
    for path in (installer, sig):
        local = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if published.get(path.name) != local:
            raise ReleaseError(
                f"{tag} IS PUBLISHED but the uploaded {path.name} ({published.get(path.name)}) does not match "
                f"the local file ({local}). Fix it before anyone updates: re-sign and `gh release upload {tag} --clobber`.")
    if not update_signing.verify(installer.read_bytes(), sig.read_bytes(), update_signing.embedded_public_key()):
        raise ReleaseError(f"{tag} IS PUBLISHED but its signature does not verify against the key embedded in the app.")
    print(f"verified: published {installer.name} and its signature match and verify")


def _release_files_present() -> list[str]:
    return [f for f in guard.RELEASE_FILES if (ROOT / f).exists()]


def _run_release(args: argparse.Namespace, journal: guard.Journal) -> None:
    version, tag = args.version, f"v{args.version}"
    title = args.title
    notes = args.notes or title
    state: dict = journal.data

    def do(name: str, fn) -> None:
        if journal.is_done(name):
            print(f"\n=== {name}: already done — skipping ===")
            return
        step(name)
        fn()

    # 1. bump ---------------------------------------------------------------
    def bump():
        bump_cargo_toml(version)
        bump_tauri_conf(version)
        bump_android_gradle(version)
        c = guard.sync_cargo_lock(version, ROOT)
        print(f"Cargo.lock: {c.detail or 'already in sync'}")
        journal.done("bumped")
    do("bumped", bump)

    # 2. commit -------------------------------------------------------------
    def commit():
        assert_head = state["head"]
        if guard.head_sha(ROOT) != assert_head:
            raise ReleaseError("HEAD moved before the release commit")
        changed = set(guard.git("diff", "--name-only", root=ROOT).output.split())
        extra = changed - set(guard.RELEASE_FILES)
        if extra:
            raise ReleaseError("tracked files other than the version bumps changed, refusing to sweep "
                               "them into the release commit: " + ", ".join(sorted(extra)))
        must(["git", "add", *_release_files_present()], what="git add")
        must(["git", "commit", "-m", f"Release {tag}"], what="git commit")
        head = guard.head_sha(ROOT)
        state["head"] = head
        journal.done("committed", release_commit=head, head=head)
    do("committed", commit)

    # 3. gate ---------------------------------------------------------------
    def gate():
        assert_stable(state["head"])
        if args.skip_gate:
            print("!! --skip-gate: NOT running the full pipeline before tagging — the pre-push hook will run it instead")
        else:
            py = ROOT / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
            cmd = [str(py if py.exists() else sys.executable), str(ROOT / "scripts" / "local_pipeline.py"), "--no-deploy"]
            print(f"+ {' '.join(cmd)}")
            res = guard.run_cmd(cmd, cwd=ROOT, timeout=GATE_TIMEOUT)
            if not res.ok:
                raise ReleaseError("the pipeline gate failed — nothing was tagged or pushed "
                                   "(see logs/local_pipeline for the full log)")
            state["gated_sha"] = state["head"]
        journal.done("gated")
    do("gated", gate)
    if state.get("gated_sha") and state["gated_sha"] == guard.head_sha(ROOT):
        os.environ[guard.PIPELINE_PASSED_ENV] = state["gated_sha"]

    # 4. desktop build + smoke ----------------------------------------------
    def desktop():
        assert_stable(state["head"])
        r = guard.heal_locks(ROOT)
        if not r.ok:
            raise ReleaseError("build outputs are still locked by something outside this repo: "
                               + ", ".join(r.still_locked[:4]))
        installer = build_desktop(version)
        journal.done("built_desktop", installer=str(installer))
    do("built_desktop", desktop)
    installer = Path(state["installer"])

    def smoke():
        ok, detail = guard.smoke_test_bundle(DESKTOP_DIR / "stage")
        if not ok:
            raise ReleaseError("the staged installer bundle does not start: " + detail)
        print(f"bundle smoke test: {detail}")
        guard.heal_locks(ROOT)
        journal.done("smoke")
    do("smoke", smoke)

    # 5. android ---------------------------------------------------------------
    def android():
        apk = build_android()
        journal.done("built_android", apk=str(apk))
    do("built_android", android)
    apk = Path(state["apk"])

    # 6. tag + push ------------------------------------------------------------
    def tag_step():
        assert_stable(state["head"])
        must(["git", "tag", tag], what="git tag")
        journal.done("tagged")
    do("tagged", tag_step)

    def push():
        assert_stable(state["head"])
        retrying(["git", "push"], attempts=4, base_delay=5, timeout=GATE_TIMEOUT, what="git push")
        journal.done("pushed")
    do("pushed", push)

    def push_tag():
        retrying(["git", "push", "origin", tag], attempts=4, base_delay=5, timeout=600, what="git push tag")
        journal.done("tag_pushed")
    do("tag_pushed", push_tag)

    # 7. sign + publish --------------------------------------------------------
    # Sign AFTER the pushes, immediately before uploading: nothing builds between
    # this signature and the upload, so the signature always matches the bytes
    # that are uploaded.
    def publish():
        inst = current_installer(installer)
        try:
            sig = update_signing.sign_installer(inst)
        except (FileNotFoundError, ValueError, TypeError) as exc:
            raise ReleaseError(f"can't sign the update installer: {exc}") from exc
        print(f"update signature: {sig}")
        state["sig"] = str(sig)

        def command() -> list[str]:
            # Re-decided per attempt: a create that half-succeeded before a
            # network drop must be completed with an upload, not re-created.
            if release_exists(tag):
                return ["gh", "release", "upload", tag, str(inst), str(sig), str(apk), "--clobber"]
            return ["gh", "release", "create", tag, "--title", title, "--notes", notes,
                    str(inst), str(sig), str(apk)]
        retrying(command, attempts=4, base_delay=5, timeout=1800, what="gh release")
        journal.done("released")
    do("released", publish)

    def verify():
        verify_published_assets(tag, current_installer(installer), Path(state["sig"]))
        journal.done("verified")
    do("verified", verify)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Cut and publish a release (see the module docstring).")
    p.add_argument("version")
    p.add_argument("title")
    p.add_argument("notes", nargs="?", default=None)
    p.add_argument("--resume", action="store_true", help="continue an interrupted release from its last completed step")
    p.add_argument("--skip-gate", action="store_true", help="don't run the full pipeline before tagging (not recommended)")
    p.add_argument("--dry-run", action="store_true", help="run the pre-flight checks and stop")
    args = p.parse_args(argv)

    validate_version(args.version)
    tag = f"v{args.version}"
    # Windows consoles default to a legacy code page that turns the dashes in
    # our messages into garbage (or raises); the log file is UTF-8 anyway.
    for stream in (sys.__stdout__, sys.__stderr__):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.stdout = sys.stderr = _Tee(ROOT / "logs" / "release" / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{tag}.log")

    try:
        with guard.PipelineLock(LOCK_PATH, f"release {tag}"):
            _main_locked(args)
    except guard.Busy as exc:
        die(str(exc))


def _main_locked(args: argparse.Namespace) -> None:
    version, tag = args.version, f"v{args.version}"
    print(f"\n=== Publishing AgenticBotPlatform {tag} ===")

    journal = guard.Journal.load(JOURNAL_PATH)
    if journal and not args.resume:
        # A previous run died mid-flight. Undo what can be undone before
        # starting over; if it had already pushed, don't guess — resume.
        step(f"recovering from an interrupted release of v{journal.version}")
        for action in guard.rollback(journal, ROOT):
            print(f"  {action}")
        if journal.is_done("pushed"):
            die(f"v{journal.version} was interrupted AFTER pushing. Finish it with the same command plus "
                f"--resume (or delete {JOURNAL_PATH.name} if you have dealt with it by hand).")
        journal.clear()
        journal = None
    if args.resume:
        if not journal or journal.version != version:
            die(f"nothing to resume for {tag} (no matching {JOURNAL_PATH.name})")
        if guard.head_sha(ROOT) != journal.data.get("head"):
            die("HEAD is not where the interrupted release left it — resolve that first")

    step("Pre-flight")
    checks = guard.run_preflight(version, ROOT, resume=args.resume)
    heal = guard.heal_locks(ROOT)
    checks.append(guard.Check("build outputs are not locked", heal.ok,
                              ", ".join(heal.still_locked[:3]) or (f"stopped {', '.join(heal.stopped)}" if heal.stopped else ""),
                              "close whatever holds them", fixed=bool(heal.stopped) and heal.ok))
    if not args.resume:
        checks.append(guard.sync_cargo_lock(version, ROOT, apply=False))  # report only; the bump step applies it
    blocking = guard.report(checks)
    if blocking:
        die(f"pre-flight failed ({len(blocking)} problem(s)) — nothing was changed")
    if args.dry_run:
        print("\npre-flight passed (--dry-run: stopping here)")
        return

    if journal is None:
        base = guard.head_sha(ROOT)
        journal = guard.Journal(JOURNAL_PATH, version, base)
        journal.data["head"] = base
        journal.save()

    try:
        _run_release(args, journal)
    except (ReleaseError, SystemExit, KeyboardInterrupt, Exception) as exc:  # noqa: BLE001
        msg = str(exc) if not isinstance(exc, SystemExit) else "exited"
        print(f"\n[FAILED] {type(exc).__name__}: {msg}\n", file=sys.stderr)
        actions = guard.rollback(journal, ROOT)
        for a in actions:
            print(f"  rollback: {a}")
        if not journal.is_done("pushed"):
            journal.clear()
        raise SystemExit(1)

    journal.clear()
    installer = Path(journal.data["installer"])
    apk = Path(journal.data["apk"])
    print(f"\n=== {tag} published ===")
    print(f"  desktop: {installer.name}")
    print(f"  android: {apk.name}")
    print("Verify: the desktop app's Updates panel should now offer a working download,")
    print("and Android's in-app update check should offer this release's APK.")


if __name__ == "__main__":
    main()
