"""Which commit is the running app actually executing?

A release build runs the Python bundled next to its own exe, so the code in
the checkout and the code answering a request are two different things — and
nothing in the ordinary answer ("git says HEAD is current") can tell them
apart. That is how a running app kept serving the Python from days earlier
while every restart "succeeded".

So the build records what it was built from (scripts/stage_bundle.py's
stamp, shipped as a bundle resource), and the app reports it at boot and on
/healthz (bot/diagnostics.py, bot/main.py). These tests are about that
reporting: a real throwaway git repository, real files, and the real FastAPI
app.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stage_bundle as sb  # noqa: E402

from bot import diagnostics  # noqa: E402
from bot.dashboard.server import build_app  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repository with one commit, and a bundle holding a stamp
    pointing at some other one."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _stamp(bundle: Path, **fields) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / diagnostics.BUILD_STAMP_NAME).write_text(json.dumps(fields), encoding="utf-8")


@pytest.fixture
def reporting(repo, bundle, monkeypatch):
    """Points the reporter at `bundle` (where the code being run lives) and
    `repo` (the checkout it was started from)."""
    monkeypatch.setattr(diagnostics, "BUNDLE_ROOT", bundle)
    monkeypatch.setattr(diagnostics, "CODE_ROOT", repo)
    diagnostics.build_status.cache_clear()
    yield repo, bundle
    diagnostics.build_status.cache_clear()


@pytest.fixture
def bundle(tmp_path):
    return tmp_path / "bundle"


# --------------------------------------------------------------------------- #
# The stamp: written at build time, shipped as a resource
# --------------------------------------------------------------------------- #
def test_the_build_stamp_records_the_checkout_this_bundle_was_built_from(monkeypatch, tmp_path):
    real = tmp_path / "fake-root"
    (real / "bot").mkdir(parents=True)
    (real / "bot" / "__init__.py").write_text('__version__ = "0.0.0"\n', encoding="utf-8")
    _git(real, "init", "-q", "-b", "main")
    _git(real, "config", "user.email", "t@example.invalid")
    _git(real, "config", "user.name", "t")
    (real / "bot" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _git(real, "add", "-A")
    _git(real, "commit", "-q", "-m", "base")
    monkeypatch.setattr(sb, "ROOT", real)

    stage = tmp_path / "stage"
    stage.mkdir()
    path = sb.write_build_stamp(stage)
    stamp = json.loads(path.read_text(encoding="utf-8"))
    assert stamp["commit"] == _git(real, "rev-parse", "HEAD")
    assert stamp["dirty"] is False
    assert stamp["built_at"]
    assert path.name == sb.BUILD_STAMP


def test_a_build_outside_git_says_so_rather_than_guessing(monkeypatch, tmp_path):
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    monkeypatch.setattr(sb, "ROOT", plain)
    stamp = json.loads(sb.write_build_stamp(tmp_path / "stage").read_text(encoding="utf-8"))
    assert stamp["commit"] == "" and stamp["commit_date"] == ""


def test_the_stamp_is_shipped_as_a_bundle_resource_so_it_reaches_the_install():
    resources = json.loads((ROOT / "desktop-app" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["bundle"]["resources"]
    assert resources.get(f"stage/{sb.BUILD_STAMP}") == sb.BUILD_STAMP, \
        "without a resource entry the stamp never reaches the folder the app runs from"
    # And stage_bundle.py really writes it, so the entry can never go stale.
    assert sb.BUILD_STAMP in (ROOT / "scripts" / "stage_bundle.py").read_text(encoding="utf-8")


def test_release_guard_rejects_a_stage_without_the_stamp(tmp_path):
    """tauri_build validates every resource source, so a stage built before
    this existed makes the Rust check fail until stage_bundle.py re-runs."""
    import release_guard

    desk = tmp_path / "desktop-app" / "src-tauri"
    (desk / "stage" / "bot").mkdir(parents=True)
    (desk / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": {"stage/bot": "bot", f"stage/{sb.BUILD_STAMP}": sb.BUILD_STAMP}}}),
        encoding="utf-8")
    assert release_guard.missing_bundle_resources(tmp_path) == [f"stage/{sb.BUILD_STAMP}"]
    (desk / "stage" / sb.BUILD_STAMP).write_text("{}", encoding="utf-8")
    assert release_guard.missing_bundle_resources(tmp_path) == []


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def test_a_bundle_built_from_this_checkout_is_not_stale(reporting):
    repo, bundle = reporting
    _stamp(bundle, commit=_git(repo, "rev-parse", "HEAD"), commit_date="2026-01-01", built_at="2026-01-01T00:00:00+00:00")
    status = diagnostics.build_status()
    assert status["commit"] == status["checkout_commit"] and len(status["commit"]) == 8
    assert status["stale"] is False
    assert status["commit_date"] == "2026-01-01"


def test_a_bundle_built_from_another_commit_is_stale(reporting):
    """The whole point: the checkout is at HEAD and the running code is not."""
    repo, bundle = reporting
    (repo / "README.md").write_text("moved on\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "moved on")
    _stamp(bundle, commit="0" * 40, dirty=True)
    status = diagnostics.build_status()
    assert status["stale"] is True
    assert status["commit"] != status["checkout_commit"]
    assert status["dirty"] is True


def test_an_install_with_no_checkout_behind_it_is_not_stale_it_is_only_unknown(reporting, monkeypatch, tmp_path):
    repo, bundle = reporting
    _stamp(bundle, commit=_git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(diagnostics, "CODE_ROOT", tmp_path / "not-a-repo")
    diagnostics.build_status.cache_clear()
    assert diagnostics.build_status()["stale"] is None
    assert diagnostics.build_status()["commit"], "the bundle's own commit is still known"


def test_a_bundle_with_no_stamp_at_all_still_answers(reporting):
    repo, bundle = reporting
    assert diagnostics.build_status()["commit"] == ""
    assert diagnostics.build_status()["checkout_commit"] == _git(repo, "rev-parse", "HEAD")[:8]
    assert diagnostics.build_status()["stale"] is None


@pytest.mark.parametrize("junk", ["not json at all", "[]", ""])
def test_an_unreadable_stamp_is_an_empty_record_not_a_crash(reporting, junk):
    repo, bundle = reporting
    _stamp(bundle)
    (bundle / diagnostics.BUILD_STAMP_NAME).write_text(junk, encoding="utf-8")
    assert diagnostics.read_build_stamp() == {}
    diagnostics.build_status.cache_clear()
    assert diagnostics.build_status()["stale"] is None


# --------------------------------------------------------------------------- #
# Where the report shows up
# --------------------------------------------------------------------------- #
def test_healthz_says_which_commit_is_answering(temp_db, reporting):
    repo, bundle = reporting
    _stamp(bundle, commit=_git(repo, "rev-parse", "HEAD"))
    body = TestClient(build_app()).get("/healthz").json()
    assert body["status"] == "ok"
    assert body["bundle"]["commit"] == body["bundle"]["checkout_commit"]
    assert body["bundle"]["stale"] is False


def test_healthz_flags_a_stale_bundle(temp_db, reporting):
    repo, bundle = reporting
    _stamp(bundle, commit="2" * 40)
    body = TestClient(build_app()).get("/healthz").json()
    assert body["bundle"]["stale"] is True
    assert body["bundle"]["commit"] != body["bundle"]["checkout_commit"]


def test_healthz_survives_a_bundle_with_no_stamp(temp_db):
    body = TestClient(build_app()).get("/healthz").json()
    assert body["status"] == "ok" and set(body["bundle"]) >= {"commit", "checkout_commit", "stale"}


def test_the_overview_reports_the_bundle_separately_from_the_checkout(temp_db, reporting, monkeypatch):
    """app_commit is the CHECKOUT's HEAD and always looked current; the pair
    is what tells you whether the running code is too."""
    from bot.dashboard.routes import reads

    repo, bundle = reporting
    _stamp(bundle, commit="3" * 40)
    reads._build_info.cache_clear()
    try:
        info = reads._build_info()
        assert info["bundle_commit"] == "33333333"
        assert info["bundle_stale"] is True
        assert info["app_commit"] and info["app_commit"] != info["bundle_commit"]
        # ...and both reach the route.
        monkeypatch.setenv("DASHBOARD_TOKEN", "unused")
        body = TestClient(build_app()).get("/api/overview", headers={"X-Dashboard-Token": "unused"}).json()
        assert "bundle_commit" in body and "bundle_stale" in body
    finally:
        reads._build_info.cache_clear()


def test_startup_logs_a_warning_when_the_running_bundle_is_stale(reporting, caplog):
    import logging

    from bot import main

    repo, bundle = reporting
    _stamp(bundle, commit="4" * 40)
    with caplog.at_level(logging.INFO, logger="bot.main"):
        main._log_build_provenance()
    warned = [r for r in caplog.records if r.name == "bot.main" and r.levelno >= logging.WARNING]
    assert warned, f"a stale bundle must be shouted about at every boot, got: {[r.getMessage() for r in caplog.records]}"
    assert "STALE" in warned[0].getMessage() and "deploy_local.py" in warned[0].getMessage()


def test_startup_logs_the_commit_being_run_when_it_is_current(reporting, caplog):
    import logging

    from bot import main

    repo, bundle = reporting
    diagnostics.build_status.cache_clear()
    _stamp(bundle, commit=_git(repo, "rev-parse", "HEAD"))
    with caplog.at_level(logging.INFO, logger="bot.main"):
        main._log_build_provenance()
    mine = [r for r in caplog.records if r.name == "bot.main"]
    assert [r for r in mine if r.levelno >= logging.WARNING] == []
    assert any("running bundle" in r.getMessage() for r in mine)
