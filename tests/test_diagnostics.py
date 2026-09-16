"""bot/diagnostics.py — telemetry counters, crash reports, and the
exportable support bundle. Everything here is local-only by design (no
telemetry endpoint, no network call in the module); these tests confirm
the on-disk artifacts a human would actually attach to a bug report are
correct, not that anything gets sent anywhere.
"""
from __future__ import annotations

import json
import logging
import zipfile

from bot import diagnostics


def _make_record(level=logging.CRITICAL, exc_info=None, msg="boom", logger_name="test.diag"):
    return logging.LogRecord(
        name=logger_name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


def test_write_crash_report_creates_a_json_file_with_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")

    try:
        raise ValueError("something broke")
    except ValueError:
        import sys

        record = _make_record(exc_info=sys.exc_info())

    report_id = diagnostics.write_crash_report(record)

    assert report_id is not None
    path = diagnostics.CRASH_DIR / f"{report_id}.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["exception_type"] == "ValueError"
    assert "something broke" in data["traceback"]
    assert data["level"] == "CRITICAL"


def test_write_crash_report_never_raises_on_a_broken_record(tmp_path, monkeypatch):
    # No CRASH_DIR patch — point it somewhere unwritable-ish by using a
    # path that can't be created (a file where a directory is expected).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "CRASH_DIR", blocker / "crash_reports")

    record = _make_record()
    result = diagnostics.write_crash_report(record)

    assert result is None  # failed cleanly, no exception escaped


def test_diagnostics_handler_only_reports_critical_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    handler = diagnostics._DiagnosticsHandler()

    handler.emit(_make_record(level=logging.ERROR))
    assert not diagnostics.CRASH_DIR.exists() or not list(diagnostics.CRASH_DIR.glob("*.json"))

    handler.emit(_make_record(level=logging.CRITICAL))
    assert list(diagnostics.CRASH_DIR.glob("*.json"))


def test_diagnostics_handler_counts_warning_and_error_telemetry(monkeypatch):
    monkeypatch.setattr(diagnostics, "telemetry", diagnostics._Telemetry())
    handler = diagnostics._DiagnosticsHandler()

    handler.emit(_make_record(level=logging.WARNING, logger_name="test.warn"))
    handler.emit(_make_record(level=logging.ERROR, logger_name="test.err"))

    counters = diagnostics.telemetry.snapshot()["counters"]
    assert counters["log.warning"] == 1
    assert counters["log.warning.test.warn"] == 1
    assert counters["log.error"] == 1
    assert counters["log.error.test.err"] == 1


def test_crash_reports_are_pruned_past_the_max(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    monkeypatch.setattr(diagnostics, "MAX_CRASH_REPORTS", 3)

    import time

    for i in range(5):
        diagnostics.write_crash_report(_make_record(msg=f"crash {i}"))
        time.sleep(0.002)  # report ids are timestamp-based — keep them ordered

    assert len(list(diagnostics.CRASH_DIR.glob("*.json"))) == 3


def test_get_crash_report_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    diagnostics.CRASH_DIR.mkdir(parents=True)
    (diagnostics.CRASH_DIR.parent / "secret.json").write_text("{}", encoding="utf-8")

    assert diagnostics.get_crash_report("../secret") is None
    assert diagnostics.get_crash_report("..\\secret") is None
    assert diagnostics.get_crash_report("a/b") is None


def test_list_crash_reports_reads_back_what_was_written(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")

    report_id = diagnostics.write_crash_report(_make_record(msg="specific crash message"))

    reports = diagnostics.list_crash_reports()
    assert any(r["id"] == report_id and r["message"] == "specific crash message" for r in reports)


def test_build_support_bundle_contains_expected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "CRASH_DIR", tmp_path / "crash_reports")
    monkeypatch.setattr(diagnostics, "BUNDLE_DIR", tmp_path / "support_bundles")
    monkeypatch.setattr(diagnostics, "LOG_DIR", tmp_path)

    diagnostics.write_crash_report(_make_record(msg="bundled crash"))

    bundle_path = diagnostics.build_support_bundle()

    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as zf:
        names = zf.namelist()
        assert "system_info.json" in names
        assert "telemetry.json" in names
        assert any(n.startswith("crash_reports/") for n in names)


def test_telemetry_snapshot_reports_counters_and_events():
    t = diagnostics._Telemetry()
    t.increment("platform.crash")
    t.increment("platform.crash")
    t.record_event("platform_crash", "bot-x crashed")

    snap = t.snapshot()
    assert snap["counters"]["platform.crash"] == 2
    assert snap["recent_events"][-1]["category"] == "platform_crash"
    assert snap["uptime_s"] >= 0
