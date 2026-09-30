"""Shared pytest fixtures.

`temp_db` points bot.db at a fresh, throwaway SQLite file for the
duration of one test — never the real data/bot.db. This is the same
monkeypatch-DB_PATH-and-reset-the-cached-connection pattern used by every
ad-hoc verification script earlier in this project's history, just made
reusable and permanent instead of hand-written and discarded each time.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Must run BEFORE any `from bot...` import anywhere in the test session —
# bot/support_bot/hybrid.py loads its persisted classifier state from
# bot.support_bot.model_io.CURRENT_PATH once, at module import time, not
# per-call. Without this override, a real data/support_bot_models/current.json
# left behind by live desktop testing in this same checkout would
# silently win over what every Support Bot test expects to be trained
# fresh from the current bot/support_bot/training_data.py. See
# model_io.py's own comment on CURRENT_PATH for the full explanation.
# A fresh directory per test process (each pytest-xdist worker is its own
# process): nothing persists between runs, so one run's retrained model can
# never leak into the next, and parallel workers never share a file. Set
# unconditionally (not setdefault): a worker inherits the controller's
# environment and must still get its own directory.
_STATE = Path(tempfile.mkdtemp(prefix="abp-pytest-"))
atexit.register(shutil.rmtree, _STATE, ignore_errors=True)
os.environ["AGENTICBOTPLATFORM_SUPPORT_BOT_MODEL_PATH"] = str(_STATE / "support_bot_model.json")
# Same rationale, for bot/support_bot/module_manifest.py's per-Knowledge-
# Module persisted models and manifest — see that module's own comment.
os.environ["AGENTICBOTPLATFORM_SUPPORT_BOT_MODULES_DIR"] = str(_STATE / "support_bot_modules")
os.environ["AGENTICBOTPLATFORM_SUPPORT_BOT_MANIFEST_PATH"] = str(_STATE / "support_bot_manifest.json")

import pytest

from bot import db as db_module


_COMMITTED: dict = {}


def _committed_backends():
    """The committed config/backends.yaml, read once. A checkout that also runs ABP keeps its live settings in that same
    file (a linked skill library, router models, ...), and tests must see the shipped defaults, not this machine's."""
    if "text" not in _COMMITTED:
        import subprocess

        text = None
        try:
            r = subprocess.run(["git", "show", "HEAD:config/backends.yaml"], cwd=Path(__file__).resolve().parent.parent,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
            text = r.stdout if r.returncode == 0 and r.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            pass
        _COMMITTED["text"] = text
    return _COMMITTED["text"]


_WORKER_DB = _STATE / "bot.db"
_worker_db_ready = False


@pytest.fixture(autouse=True)
def _isolated_cicd_event_store(monkeypatch, tmp_path):
    """Instrumented scripts (release, pipeline) record into the CI/CD event
    store. No test may ever write into the real one, so every test gets its own."""
    # The main database: a test that does not ask for temp_db must not use the checkout's real data/bot.db either (a
    # fresh clone has none, so its tables were missing - Linux VM runs failed on it; a developer's holds real data).
    # One initialised file per test process; temp_db, when asked for, still gives a test a fresh one of its own.
    global _worker_db_ready
    monkeypatch.setattr(db_module, "DB_PATH", _WORKER_DB)
    monkeypatch.setattr(db_module, "_conn", None)
    if not _worker_db_ready:
        db_module.init_db()
        _worker_db_ready = True
    monkeypatch.setenv("ABP_CICD_DB", str(tmp_path / "cicd-events.db"))
    # Same for agent traces (bot/agent_runtime/trace.py): every native-agent turn records one.
    monkeypatch.setenv("ABP_AGENT_TRACE_DB", str(tmp_path / "agent-traces.db"))
    # Providers served by running module hubs (ModelMistress, octopus-router) are real processes on the developer's
    # machine; tests that want them clear this (tests/test_module_providers.py, tests/test_octopus.py).
    monkeypatch.setenv("ABP_NO_MODULE_PROVIDERS", "1")
    monkeypatch.setenv("ABP_AGENT_STATE_DIR", str(tmp_path / "agent-state"))
    monkeypatch.delenv("ABP_CICD_RUN", raising=False)
    # The model catalog (models.dev) is a downloaded cache; a test must never see the developer's copy.
    from bot import model_catalog, model_pricing
    from bot.agent_runtime import context_window

    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "models-dev-cache.json")
    monkeypatch.setitem(model_pricing._memory_cache, "data", None)
    monkeypatch.setattr(model_catalog, "_disk", {"mtime": None, "data": None})
    context_window._catalog_windows.clear()
    # The provider store keeps removed providers and their (encrypted) keys; a test must not write to the real
    # one, nor create a vault key in the real data folder.
    from bot import provider_store

    monkeypatch.setattr(provider_store, "STORE_PATH", tmp_path / "provider-store.db")
    monkeypatch.setattr(provider_store, "_history_summaries", lambda: [])
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    # Config files: a test may never write the checkout's real config/providers.yaml (it holds the developer's
    # provider API keys; fake test providers were found leaked into it) or config/backends.yaml. Each test gets
    # private copies; what it reads is unchanged.
    import shutil as _shutil

    from bot import providers as _providers
    from bot.config import config as _config

    prov = tmp_path / "isolated-providers.yaml"
    monkeypatch.setattr(_providers, "PROVIDERS_PATH", prov)
    monkeypatch.setattr(_providers._manager, "path", prov)
    monkeypatch.setattr(_providers._manager, "_data", {})
    backends = tmp_path / "isolated-backends.yaml"
    committed = _committed_backends()
    if committed is not None:
        backends.write_text(committed, encoding="utf-8")
    elif Path(_config.path).is_file():
        _shutil.copy2(_config.path, backends)
    monkeypatch.setattr(_config, "path", backends)
    # ...and the in-memory copy must match that file: otherwise config.current still holds whatever the previous test
    # wrote, and a test reading it sees another test's settings (an importer test did, and wrote nothing because of it).
    monkeypatch.setattr(_config, "_data", _config._read_yaml() if backends.is_file() else {})
    # The Sentinel's journal, backups, scan results and boot records: never the real data folder.
    from bot.sentinel import backup as s_backup, bootguard as s_boot, bug_hunter as s_bugs, cve as s_cve
    from bot.sentinel import journal as s_journal, security as s_security

    sdir = tmp_path / "sentinel"
    monkeypatch.setattr(s_journal, "SENTINEL_DIR", sdir)
    monkeypatch.setattr(s_backup, "BACKUPS_ROOT", tmp_path / "backups")
    monkeypatch.setattr(s_cve, "RESULTS_PATH", sdir / "cve.json")
    monkeypatch.setattr(s_cve, "CACHE_PATH", sdir / "osv-cache.json")
    monkeypatch.setattr(s_security, "MANIFEST_PATH", sdir / "code-manifest.json")
    monkeypatch.setattr(s_boot, "BOOTS_PATH", sdir / "boots.json")
    monkeypatch.setattr(s_boot, "LKG_DIR", sdir / "last-known-good")
    monkeypatch.setenv("ABP_ANDROID_KEYSTORE", str(tmp_path / "no-keystore"))
    monkeypatch.setenv("ABP_INSTALL_POINTER", str(tmp_path / "abp-install.json"))
    s_bugs._reset_for_tests(sdir / "issues.json")
    s_journal._reset_for_tests()
    s_boot._reset_for_tests()


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_module, "_conn", None)
    conn = db_module.get_conn()
    db_module.init_db()
    # bot_instances.BACKUP_DIR is a module-level constant pointing at the
    # REAL data/bot_instances_backups/ — not test-isolated by the DB-path
    # patch above. Without this, every test that creates/updates/deletes a
    # bot instance (a lot of them) leaks a real JSON file into the shared
    # project directory forever. Confirmed: this was unpatched for the
    # project's entire history and left 47,000+ files there, which in turn
    # made the dashboard's Bots-tab backups table (no row cap) render an
    # enormous DOM and made the whole app sluggish to resize/scroll.
    from bot import bot_instances as bot_instances_module

    monkeypatch.setattr(bot_instances_module, "BACKUP_DIR", tmp_path / "bot_instances_backups")
    yield conn
    conn.close()
    monkeypatch.setattr(db_module, "_conn", None)
