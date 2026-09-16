"""bot/config.py's ConfigManager — the hot-reload path every part of
AgenticBotPlatform trusts to never hand back a broken config. Covers the one real
gap found while hardening it for live development: a YAML file that
parses fine (so the existing try/except around yaml.safe_load never
fires) but has the wrong root shape (a list instead of a mapping) used
to get swapped straight into `_data`, ready to raise deep inside whatever
code path first calls `.get()` on it.
"""

from __future__ import annotations

import asyncio

from bot.config import ConfigManager


def test_reload_rejects_non_mapping_root(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)
    assert manager.current["default_backend"] == "cli"

    path.write_text("- this\n- is\n- a list\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert "mapping" in summary
    # The bad edit never took effect — readers still see the last good config.
    assert manager.current["default_backend"] == "cli"


def test_reload_rejects_scalar_root(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("just a plain string\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert manager.current["default_backend"] == "cli"


def test_reload_still_accepts_a_good_edit(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("default_backend: api\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is True
    assert manager.current["default_backend"] == "api"


def test_reload_still_rejects_invalid_yaml_syntax(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("default_backend: [unclosed\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert manager.current["default_backend"] == "cli"


def test_read_raw_bypasses_cache(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    # Edit the file without going through reload()/set_value() — read_raw()
    # must still see it, unlike .current (the cached in-memory copy).
    path.write_text("default_backend: api\n", encoding="utf-8")
    assert manager.current["default_backend"] == "cli"
    assert manager.read_raw()["default_backend"] == "api"


def test_watch_forever_restarts_after_awatch_crashes(tmp_path, monkeypatch):
    """reload() itself never raises (proven by the tests above), so the
    real risk in watch_forever() is watchfiles.awatch() dying outright —
    a real, documented failure mode (a deleted watched file, a
    permission change, an OS-level file-watching backend hiccup) that
    used to leave config file changes silently, permanently unnoticed
    for the rest of the process's life. Confirms the outer restart loop
    actually restarts instead of just letting that exception end the
    whole background task."""
    import watchfiles

    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    call_count = {"n": 0}

    async def flaky_awatch(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated watcher crash")
            yield  # pragma: no cover - unreachable, makes this a generator function
        yield {(1, str(path))}
        await asyncio.sleep(10)  # block "forever" — the test cancels the task

    monkeypatch.setattr(watchfiles, "awatch", flaky_awatch)

    async def _run():
        task = asyncio.create_task(manager.watch_forever())
        # One real crash + the module's own ~2s backoff before the retry
        # actually runs — deliberately not mocking that sleep here since
        # it lives on the same shared `asyncio` module this test's own
        # polling loop uses, and overriding it globally would make the
        # fake awatch's own "block forever" sleep resolve instantly too,
        # racing this loop instead of behaving like a real long-lived
        # watch. A few real seconds is an acceptable, non-flaky price for
        # a single test.
        for _ in range(400):
            if call_count["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert call_count["n"] == 2  # 1 crash + 1 restart that actually ran
