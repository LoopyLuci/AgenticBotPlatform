"""bot/hotreload.py's run_cycle() — the actual reload mechanics, driven
against a small throwaway package under tmp_path (never the real bot/*.py
files, since some of those genuinely hold live process state and this
suite must never risk mutating it as a side effect of testing).
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from bot import hotreload


def _run(coro):
    return asyncio.run(coro)


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture
def fake_pkg(tmp_path, monkeypatch):
    """A tiny two-module throwaway package: fakepkg.leaf (no deps) and
    fakepkg.top (imports leaf) — mirrors the shape of a real Tier 3
    dependency edge without touching anything under bot/."""
    pkg_root = tmp_path / "fakepkg"
    pkg_root.mkdir()
    (pkg_root / "__init__.py").write_text("", encoding="utf-8")
    _write(pkg_root / "leaf.py", """
        VALUE = "original"
    """)
    _write(pkg_root / "top.py", """
        from fakepkg import leaf

        def get_value():
            return leaf.VALUE
    """)
    monkeypatch.syspath_prepend(str(tmp_path))
    import fakepkg.leaf  # noqa: F401
    import fakepkg.top  # noqa: F401

    yield pkg_root

    for mod in ("fakepkg.leaf", "fakepkg.top", "fakepkg"):
        sys.modules.pop(mod, None)
    hotreload._degraded = None
    hotreload._last_events.clear()


def _run_cycle(changed_files, pkg_root, **kwargs):
    return _run(hotreload.run_cycle(
        changed_files, pkg_root=pkg_root, pkg_dotted_prefix="fakepkg",
        denylist=frozenset(), reload_order=("fakepkg.leaf", "fakepkg.top"),
        platform_modules={}, **kwargs,
    ))


def test_good_edit_applies_and_is_visible(fake_pkg):
    import fakepkg.top

    assert fakepkg.top.get_value() == "original"
    _write(fake_pkg / "leaf.py", 'VALUE = "updated"\n')
    result = _run_cycle([fake_pkg / "leaf.py"], fake_pkg)
    assert result["status"] == "applied"
    assert fakepkg.top.get_value() == "updated"


def test_syntax_error_aborts_without_touching_anything(fake_pkg):
    import fakepkg.top

    _write(fake_pkg / "leaf.py", "VALUE = ('unterminated\n")
    result = _run_cycle([fake_pkg / "leaf.py"], fake_pkg)
    assert result["status"] == "syntax_error"
    assert fakepkg.top.get_value() == "original"  # untouched
    assert hotreload.is_degraded() is None


def test_denylisted_file_reports_restart_required(fake_pkg):
    result = _run(hotreload.run_cycle(
        [fake_pkg / "leaf.py"], pkg_root=fake_pkg, pkg_dotted_prefix="fakepkg",
        denylist=frozenset({"fakepkg.leaf"}), reload_order=("fakepkg.leaf", "fakepkg.top"),
        platform_modules={},
    ))
    assert result["status"] == "restart_required"
    assert "fakepkg.leaf" in result["detail"]


def test_unclassified_file_reports_restart_required(fake_pkg):
    _write(fake_pkg / "extra.py", "X = 1\n")
    result = _run(hotreload.run_cycle(
        [fake_pkg / "extra.py"], pkg_root=fake_pkg, pkg_dotted_prefix="fakepkg",
        denylist=frozenset(), reload_order=("fakepkg.leaf", "fakepkg.top"),
        platform_modules={},
    ))
    assert result["status"] == "restart_required"
    assert "fakepkg.extra" in result["detail"]


def test_no_relevant_files_is_a_noop(fake_pkg):
    result = _run_cycle([fake_pkg / "__init__.py"], fake_pkg)
    assert result["status"] == "no_change"


def test_runtime_error_during_reload_sets_degraded_and_halts(fake_pkg):
    import fakepkg.top

    # A NameError at module-exec time: syntactically valid, fails on execution.
    _write(fake_pkg / "leaf.py", "VALUE = undefined_name\n")
    result = _run_cycle([fake_pkg / "leaf.py"], fake_pkg)
    assert result["status"] == "degraded"
    assert hotreload.is_degraded() is not None
    assert "fakepkg.leaf" in hotreload.is_degraded()

    # A subsequent cycle, even with a perfectly good file, must be skipped.
    _write(fake_pkg / "leaf.py", 'VALUE = "fixed"\n')
    result2 = _run_cycle([fake_pkg / "leaf.py"], fake_pkg)
    assert result2["status"] == "skipped_degraded"
    assert fakepkg.top.get_value() == "original"  # never actually reloaded


def test_backend_touch_calls_shutdown_hook(fake_pkg):
    calls = []

    async def fake_shutdown():
        calls.append("shutdown")

    _write(fake_pkg / "leaf.py", 'VALUE = "v2"\n')
    result = _run(hotreload.run_cycle(
        [fake_pkg / "leaf.py"], pkg_root=fake_pkg, pkg_dotted_prefix="fakepkg",
        denylist=frozenset(), reload_order=("fakepkg.leaf", "fakepkg.top"),
        platform_modules={}, backend_modules=frozenset({"fakepkg.leaf"}),
        shutdown_backends=fake_shutdown,
    ))
    assert result["status"] == "applied"
    assert calls == ["shutdown"]

    restarted = []

    async def fake_restart(platform_name):
        restarted.append(platform_name)
        return 2

    _write(fake_pkg / "leaf.py", 'VALUE = "v2"\n')
    result = _run(hotreload.run_cycle(
        [fake_pkg / "leaf.py"], pkg_root=fake_pkg, pkg_dotted_prefix="fakepkg",
        denylist=frozenset(), reload_order=("fakepkg.leaf", "fakepkg.top"),
        platform_modules={"fakepkg.leaf": "fake_platform"},
        restart_instances_for_platform=fake_restart,
    ))
    assert result["status"] == "applied"
    assert restarted == ["fake_platform"]
    assert "fake_platform:2" in result["detail"]
