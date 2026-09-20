"""scripts/publish_release.py's transaction: every step against a throwaway git
repo + bare origin, with only the slow/external pieces (builds, gate, smoke
test, GitHub) stubbed. Proves the release either completes, rolls back cleanly,
or can be resumed — never a half-made release commit."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import publish_release as pr  # noqa: E402
import release_guard as g  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    r = tmp_path / "repo"
    (r / "desktop-app" / "src-tauri").mkdir(parents=True)
    (r / "android-app" / "app").mkdir(parents=True)
    (r / "desktop-app/src-tauri/Cargo.toml").write_text('[package]\nname = "app"\nversion = "0.7.27"\n', encoding="utf-8")
    (r / "desktop-app/src-tauri/Cargo.lock").write_text('[[package]]\nname = "app"\nversion = "0.7.27"\n', encoding="utf-8")
    (r / "desktop-app/src-tauri/tauri.conf.json").write_text('{"version": "0.7.27"}\n', encoding="utf-8")
    (r / "android-app/app/build.gradle.kts").write_text(
        'android {\n    defaultConfig {\n        versionCode = 30\n        versionName = "0.7.27"\n    }\n}\n', encoding="utf-8")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"), ("core.autocrlf", "false")):
        _git(r, "config", k, v)
    _git(r, "remote", "add", "origin", str(origin))
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "push", "-q", "-u", "origin", "main")

    d = r / "desktop-app" / "src-tauri"
    monkeypatch.setattr(pr, "ROOT", r)
    monkeypatch.setattr(pr, "DESKTOP_DIR", d)
    monkeypatch.setattr(pr, "CARGO_TOML", d / "Cargo.toml")
    monkeypatch.setattr(pr, "TAURI_CONF", d / "tauri.conf.json")
    monkeypatch.setattr(pr, "ANDROID_GRADLE", r / "android-app/app/build.gradle.kts")
    monkeypatch.setattr(pr, "JOURNAL_PATH", r / ".release_journal.json")
    monkeypatch.setattr(pr, "LOCK_PATH", r / ".pipeline.lock")

    installer = tmp_path / "App_0.7.28_x64-setup.exe"
    installer.write_bytes(b"installer")
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"apk")
    sig = tmp_path / "App_0.7.28_x64-setup.exe.sig"
    sig.write_bytes(b"sig")

    rec = {"gh": [], "builds": 0, "gate": 0, "smoke": 0, "signed": 0}
    monkeypatch.setattr(g, "run_preflight", lambda *a, **k: [])
    monkeypatch.setattr(g, "heal_locks", lambda *a, **k: g.HealResult())

    def fake_build(version):
        rec["builds"] += 1
        return installer
    monkeypatch.setattr(pr, "build_desktop", fake_build)
    monkeypatch.setattr(pr, "build_android", lambda: apk)

    def fake_smoke(stage, **k):
        rec["smoke"] += 1
        return rec.get("smoke_result", (True, "healthz ok"))
    monkeypatch.setattr(g, "smoke_test_bundle", fake_smoke)

    real_run_cmd = g.run_cmd

    def fake_run_cmd(cmd, **kw):
        if any("local_pipeline.py" in str(c) for c in cmd):
            rec["gate"] += 1
            return g.CmdResult(rec.get("gate_rc", 0), "gate output")
        return real_run_cmd(cmd, **kw)
    monkeypatch.setattr(g, "run_cmd", fake_run_cmd)

    def fake_sign(path, key=None):
        rec["signed"] += 1
        return sig
    monkeypatch.setattr(pr.update_signing, "sign_installer", fake_sign)
    monkeypatch.setattr(pr, "release_exists", lambda tag: bool(rec["gh"]))
    monkeypatch.setattr(pr, "verify_published_assets", lambda *a, **k: rec.setdefault("verified", True))

    real_retrying = pr.retrying

    def fake_retrying(cmd, **kw):
        shown = cmd() if callable(cmd) else cmd
        if shown[0] == "gh":
            if rec.get("gh_fail"):
                raise pr.ReleaseError("gh release failed (exit 1, fatal) after 1 attempt(s)")
            rec["gh"].append(shown)
            return g.CmdResult(0, "")
        return real_retrying(cmd, **kw)
    monkeypatch.setattr(pr, "retrying", fake_retrying)
    monkeypatch.setenv(g.PIPELINE_PASSED_ENV, "placeholder")
    monkeypatch.delenv(g.PIPELINE_PASSED_ENV)

    rec.update(repo=r, origin=origin, installer=installer, apk=apk, sig=sig)
    return rec


def _args(resume=False, skip_gate=False):
    return argparse.Namespace(version="0.7.28", title="T", notes="N", resume=resume,
                              skip_gate=skip_gate, dry_run=False)


def _run(env, **kw):
    pr._main_locked(_args(**kw))


def test_a_clean_release_completes_and_leaves_no_journal(env):
    r = env["repo"]
    _run(env)
    assert _git(r, "log", "-1", "--format=%s") == "Release v0.7.28"
    assert _git(r, "tag", "-l", "v0.7.28") == "v0.7.28"
    assert "v0.7.28" in _git(r, "ls-remote", "--tags", "origin")
    assert _git(r, "rev-parse", "HEAD") == _git(r, "rev-parse", "origin/main")
    assert '"0.7.28"' in (r / "desktop-app/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    assert 'version = "0.7.28"' in (r / "desktop-app/src-tauri/Cargo.lock").read_text(encoding="utf-8")
    assert "versionCode = 31" in (r / "android-app/app/build.gradle.kts").read_text(encoding="utf-8")
    assert env["gate"] == 1 and env["smoke"] == 1 and env["verified"] is True
    assert env["gh"][0][:3] == ["gh", "release", "create"]
    assert not pr.JOURNAL_PATH.exists()
    # the pre-push hook is told this exact commit already passed
    assert g.pipeline_already_passed(r) == _git(r, "rev-parse", "HEAD")


def test_a_failing_gate_stops_before_any_tag_or_push_and_rolls_back(env):
    r = env["repo"]
    base = _git(r, "rev-parse", "HEAD")
    env["gate_rc"] = 1
    with pytest.raises(SystemExit):
        _run(env)
    assert _git(r, "rev-parse", "HEAD") == base
    assert _git(r, "tag", "-l") == ""
    assert _git(r, "status", "--porcelain", "--untracked-files=no") == ""
    assert not pr.JOURNAL_PATH.exists()
    assert env["builds"] == 0 and env["gh"] == []
    assert g.PIPELINE_PASSED_ENV not in __import__("os").environ


def test_a_bundle_that_does_not_start_is_never_tagged(env):
    r = env["repo"]
    base = _git(r, "rev-parse", "HEAD")
    env["smoke_result"] = (False, "the bundled app exited with code 1")
    with pytest.raises(SystemExit):
        _run(env)
    assert _git(r, "rev-parse", "HEAD") == base
    assert _git(r, "tag", "-l") == ""
    assert "v0.7.28" not in _git(r, "ls-remote", "--tags", "origin")


def test_an_edit_made_during_the_release_aborts_it_and_survives_the_rollback(env, monkeypatch):
    r = env["repo"]
    base = _git(r, "rev-parse", "HEAD")
    real_build = pr.build_desktop

    def edit_then_build(version):
        (r / "README.md").write_text("edited mid-release\n", encoding="utf-8")
        return real_build(version)
    monkeypatch.setattr(pr, "build_desktop", edit_then_build)
    with pytest.raises(SystemExit):
        _run(env)
    assert _git(r, "rev-parse", "HEAD") == base                      # release commit dropped
    assert _git(r, "tag", "-l") == ""                                # never tagged
    assert (r / "README.md").read_text(encoding="utf-8") == "edited mid-release\n"  # the edit is NOT lost
    assert 'version = "0.7.27"' in (r / "desktop-app/src-tauri/Cargo.toml").read_text(encoding="utf-8")


def test_a_failure_after_the_push_keeps_the_pushed_work_and_resumes(env):
    r = env["repo"]
    env["gh_fail"] = True
    with pytest.raises(SystemExit):
        _run(env)
    # pushed work is never rewritten...
    assert _git(r, "log", "-1", "--format=%s") == "Release v0.7.28"
    assert "v0.7.28" in _git(r, "ls-remote", "--tags", "origin")
    assert pr.JOURNAL_PATH.exists()
    builds = env["builds"]
    # ...and the same command with --resume finishes it without redoing anything
    env["gh_fail"] = False
    _run(env, resume=True)
    assert env["builds"] == builds and env["gate"] == 1
    assert env["gh"] and env["verified"] is True
    assert not pr.JOURNAL_PATH.exists()
    assert _git(r, "log", "--format=%s", "-3").count("Release v0.7.28") == 1


def test_a_leftover_journal_from_a_crashed_run_is_rolled_back_before_starting_over(env):
    r = env["repo"]
    base = _git(r, "rev-parse", "HEAD")
    (r / "android-app/app/build.gradle.kts").write_text("bumped by a run that died", encoding="utf-8")
    j = g.Journal(pr.JOURNAL_PATH, "0.7.28", base)
    j.data["head"] = base
    j.done("bumped")
    _run(env)  # starts over: restores the stray bump first, then releases normally
    text = (r / "android-app/app/build.gradle.kts").read_text(encoding="utf-8")
    assert "versionCode = 31" in text and "died" not in text
    assert _git(r, "log", "-1", "--format=%s") == "Release v0.7.28"


def test_resume_without_a_matching_journal_refuses(env):
    with pytest.raises(SystemExit):
        _run(env, resume=True)


def test_unrelated_tracked_changes_are_never_swept_into_the_release_commit(env, monkeypatch):
    r = env["repo"]
    real_bump = pr.bump_tauri_conf

    def bump_and_dirty(version):
        real_bump(version)
        (r / "README.md").write_text("sneaky\n", encoding="utf-8")
    monkeypatch.setattr(pr, "bump_tauri_conf", bump_and_dirty)
    with pytest.raises(SystemExit):
        _run(env)
    assert "sneaky" not in _git(r, "log", "-p", "-3")
    assert _git(r, "tag", "-l") == ""


def test_skip_gate_leaves_verification_to_the_push_hook(env):
    _run(env, skip_gate=True)
    assert env["gate"] == 0
    assert g.PIPELINE_PASSED_ENV not in __import__("os").environ


def test_a_half_created_release_is_completed_by_upload_not_recreated(env, monkeypatch):
    # first attempt "created" the release then the network dropped; the retry
    # must see it exists and upload with --clobber
    monkeypatch.setattr(pr, "release_exists", lambda tag: True)
    _run(env)
    assert env["gh"][0][:3] == ["gh", "release", "upload"] and "--clobber" in env["gh"][0]
