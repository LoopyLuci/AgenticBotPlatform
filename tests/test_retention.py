"""bot.retention's auto-VACUUM scheduling logic — made permanent from the
throwaway scratch script used to verify it when the feature was built."""
from __future__ import annotations

import asyncio

from bot import db, retention
from bot.config import config


def _set_retention_config(monkeypatch, **overrides):
    cfg = {"enabled": True, "days": 90, "auto_vacuum_every_days": 0}
    cfg.update(overrides)
    monkeypatch.setattr(config, "_data", {"retention": cfg})


def test_no_auto_vacuum_before_any_history_but_below_threshold(temp_db, monkeypatch):
    _set_retention_config(monkeypatch, auto_vacuum_every_days=7)
    db.vacuum(actor="test", detail="baseline")
    before = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]

    asyncio.run(retention.run_once())

    after = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]
    assert after == before, "must not re-vacuum before the configured interval has elapsed"


def test_auto_vacuum_fires_once_the_interval_has_elapsed(temp_db, monkeypatch):
    _set_retention_config(monkeypatch, auto_vacuum_every_days=7)
    db.vacuum(actor="test", detail="baseline")
    temp_db.execute("UPDATE audit_log SET ts='2000-01-01T00:00:00+00:00' WHERE action='vacuum'")
    temp_db.commit()
    before = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]

    asyncio.run(retention.run_once())

    after = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]
    assert after == before + 1


def test_auto_vacuum_disabled_by_default_never_fires(temp_db, monkeypatch):
    _set_retention_config(monkeypatch, auto_vacuum_every_days=0)
    db.vacuum(actor="test", detail="baseline")
    temp_db.execute("UPDATE audit_log SET ts='2000-01-01T00:00:00+00:00' WHERE action='vacuum'")
    temp_db.commit()
    before = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]

    asyncio.run(retention.run_once())

    after = temp_db.execute("SELECT COUNT(*) c FROM audit_log WHERE action='vacuum'").fetchone()["c"]
    assert after == before


def test_retention_disabled_entirely_skips_both_prune_and_vacuum(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"retention": {"enabled": False, "days": 1, "auto_vacuum_every_days": 1}})
    old = "2000-01-01T00:00:00+00:00"
    temp_db.execute("INSERT INTO telemetry_events (component, metric, ts) VALUES ('x', 'old', ?)", (old,))
    temp_db.commit()

    asyncio.run(retention.run_once())

    remaining = temp_db.execute("SELECT COUNT(*) c FROM telemetry_events").fetchone()["c"]
    assert remaining == 1, "retention.enabled=False must skip pruning entirely"
