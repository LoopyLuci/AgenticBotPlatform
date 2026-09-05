"""bot.support_bot.actions._app_update — the "update the app" intent's
real action: queues the last-built Android APK to a named device, or to
every paired device when none is named. Reuses bot.android_apk (the same
path/label helpers the dashboard's own APK-push routes use) and
db.create_apk_push (the same queue GET /api/android/apk/pending polls),
so this is exercised against real temp_db rows, not mocks.
"""

from __future__ import annotations

import pytest

from bot import android_apk, db
from bot.support_bot.actions import ActionError, _app_update


@pytest.fixture(autouse=True)
def _fake_apk(tmp_path, monkeypatch):
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"fake apk bytes")
    monkeypatch.setattr(android_apk, "latest_apk_path", lambda: apk)


def test_raises_when_no_apk_has_been_built(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(android_apk, "latest_apk_path", lambda: tmp_path / "does-not-exist.apk")

    with pytest.raises(ActionError):
        _app_update("update the app", "test")


def test_raises_when_no_devices_are_paired(temp_db):
    with pytest.raises(ActionError):
        _app_update("update the app", "test")


def test_updates_a_named_device_only(temp_db):
    # A single-word label so bot.support_bot.slots.find_device's exact
    # substring fallback matches it — its fuzzy match doesn't reliably
    # match a one-word query against a multi-word label, a pre-existing
    # characteristic of slots.py this test isn't meant to exercise.
    key_id, _ = db.create_api_key("Xiaomi", kind="device")
    other_id, _ = db.create_api_key("Nokia", kind="device")

    result = _app_update("update my xiaomi", "test")

    assert "Xiaomi" in result
    pending = db.get_pending_apk_push(key_id)
    assert pending is not None
    assert db.get_pending_apk_push(other_id) is None


def test_updates_every_paired_device_when_none_named(temp_db):
    key_id, _ = db.create_api_key("Xiaomi Phone", kind="device")
    other_id, _ = db.create_api_key("Nokia 7", kind="device")

    result = _app_update("update the app", "test")

    assert "2 paired device" in result
    assert db.get_pending_apk_push(key_id) is not None
    assert db.get_pending_apk_push(other_id) is not None


def test_skips_revoked_devices_when_updating_all(temp_db):
    key_id, _ = db.create_api_key("Xiaomi Phone", kind="device")
    revoked_id, _ = db.create_api_key("Old Phone", kind="device")
    db.revoke_api_key(revoked_id)

    result = _app_update("update the app", "test")

    assert "1 paired device" in result
    assert db.get_pending_apk_push(key_id) is not None
    assert db.get_pending_apk_push(revoked_id) is None
