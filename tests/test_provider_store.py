"""Removing a model provider must never be final: the provider store keeps it, and it can be restored.

Real files throughout (a temp providers.yaml, a temp store, a temp vault folder); keys are the plain placeholder
"unused-..." text, never key-shaped strings.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from bot import provider_store, providers
from bot.config import ConfigManager
from bot.dashboard.server import build_app

KEY = "unused-placeholder-key"


@pytest.fixture(autouse=True)
def _temp_registry(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=path))
    return path


def _statuses():
    return {p["name"]: p["status"] for p in providers.store_listing()}


def test_removing_a_provider_keeps_it_and_restoring_brings_it_back_with_its_key():
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY, catalog_id="acme-cat")
    assert providers.delete_provider("acme") is True
    assert providers.get_provider("acme") is None
    assert _statuses() == {"acme": "deleted"}

    providers.restore_provider("acme")
    restored = providers.get_provider("acme")
    assert restored["base_url"] == "https://example.test/v1"
    assert restored["catalog_id"] == "acme-cat"
    assert providers.get_api_key("acme") == KEY
    assert _statuses() == {"acme": "active"}


def test_the_listing_never_carries_the_key_only_whether_one_is_kept():
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY)
    providers.delete_provider("acme")
    (row,) = providers.store_listing("deleted")
    assert row["has_key"] is True
    assert KEY not in repr(row)


def test_the_key_is_encrypted_in_the_store_file():
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY)
    providers.delete_provider("acme")
    assert KEY.encode() not in provider_store.STORE_PATH.read_bytes()
    stored = sqlite3.connect(provider_store.STORE_PATH).execute("select key_sealed from provider_store").fetchone()[0]
    assert stored and KEY not in stored


def test_restore_can_replace_a_rotated_key():
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY)
    providers.delete_provider("acme")
    providers.restore_provider("acme", api_key="unused-new-key")
    assert providers.get_api_key("acme") == "unused-new-key"


def test_a_provider_that_used_an_env_var_restores_the_env_var():
    providers.set_provider("acme", "https://example.test/v1", api_key_env="ACME_KEY_NAME")
    providers.delete_provider("acme")
    providers.restore_provider("acme")
    assert providers.get_provider("acme")["api_key_env"] == "ACME_KEY_NAME"


def test_restore_refuses_when_the_name_is_taken_or_unknown():
    providers.set_provider("acme", "https://example.test/v1")
    providers.delete_provider("acme")
    providers.set_provider("acme", "https://other.test/v1")
    with pytest.raises(ValueError, match="already configured"):
        providers.restore_provider("acme")
    with pytest.raises(ValueError, match="no removed provider"):
        providers.restore_provider("never-existed")
    assert providers.get_provider("acme")["base_url"] == "https://other.test/v1"


def test_a_provider_removed_by_editing_the_file_is_noticed_and_kept(_temp_registry):
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY)
    _temp_registry.write_text("providers: {}\n", encoding="utf-8")
    providers.reload()
    assert _statuses() == {"acme": "deleted"}
    providers.restore_provider("acme")
    assert providers.get_api_key("acme") == KEY


def test_a_provider_added_by_editing_the_file_is_recorded(_temp_registry):
    _temp_registry.write_text("providers:\n  hand:\n    base_url: https://hand.test/v1\n    protocol: openai\n", encoding="utf-8")
    providers.reload()
    (row,) = providers.store_listing()
    assert (row["name"], row["status"], row["source"]) == ("hand", "active", "file")


def test_purge_only_forgets_a_removed_provider(temp_db):
    providers.set_provider("acme", "https://example.test/v1", api_key=KEY)
    assert provider_store.purge("acme") is False  # still active
    providers.delete_provider("acme")
    assert provider_store.purge("acme") is True
    assert providers.store_listing() == []
    with pytest.raises(ValueError):
        providers.restore_provider("acme")


def test_model_settings_survive_a_removal_and_are_cleared_by_a_purge(temp_db):
    from bot import db

    providers.set_provider("acme", "https://example.test/v1")
    db.set_model_toggle("acme", "m1", True)
    db.set_model_toggle("acme", "m2", False)
    providers.delete_provider("acme")
    (row,) = providers.store_listing("deleted")
    assert row["model_settings"] == 2
    provider_store.purge("acme")
    assert db.get_conn().execute("select count(*) from model_toggles where provider='acme'").fetchone()[0] == 0


def test_a_broken_store_never_blocks_a_config_change(monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(provider_store, "record_saved", boom)
    monkeypatch.setattr(provider_store, "record_deleted", boom)
    providers.set_provider("acme", "https://example.test/v1")
    assert providers.get_provider("acme") is not None
    assert providers.delete_provider("acme") is True
    assert providers.get_provider("acme") is None


# ------------------------------------------------------------- recovery --
def test_history_recovery_rebuilds_removed_providers_without_their_keys():
    history = [
        "providers.acme: None -> {'base_url': 'https://example.test/v1', 'protocol': 'openai', 'api_key': '<hidden>'}",
        "providers.keep: None -> {'base_url': 'https://keep.test/v1', 'protocol': 'openai'}; providers.acme: {'base_url': 'https://example.test/v1', 'protocol': 'openai', 'api_key': '<hidden>'} -> None",
        "providers.back: {'base_url': 'https://back.test/v1', 'protocol': 'openai'} -> None",
        "providers.back: None -> {'base_url': 'https://back.test/v1', 'protocol': 'openai'}",
        "router.timeout: 5 -> 9",
    ]
    found = provider_store._recover_entries(history)
    assert set(found) == {"acme"}  # keep was never removed; back was removed and added again
    assert found["acme"]["base_url"] == "https://example.test/v1"


def test_recover_from_another_install_adds_them_as_removed_and_restorable(tmp_path):
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("create table config_history (id integer primary key, ts text, version integer, actor text, summary text)")
    con.execute(
        "insert into config_history (ts, version, actor, summary) values ('t', 1, 'dashboard', ?)",
        ("providers.lost: {'base_url': 'https://lost.test/v1', 'protocol': 'openai', 'catalog_id': 'lost'} -> None",),
    )
    con.commit()
    con.close()

    assert provider_store.recover_from_db(other) == 1
    assert provider_store.recover_from_db(other) == 0  # nothing new the second time
    (row,) = provider_store.list_all("deleted")
    assert (row["name"], row["has_key"], row["source"]) == ("lost", False, "recovered")
    providers.restore_provider("lost", api_key="unused-new-key")
    assert providers.get_provider("lost")["catalog_id"] == "lost"


def test_the_first_listing_rebuilds_removed_providers_from_this_installs_own_history(monkeypatch):
    calls = []

    def history():
        calls.append(1)
        return ["providers.old: {'base_url': 'https://old.test/v1', 'protocol': 'openai'} -> None"]

    monkeypatch.setattr(provider_store, "_history_summaries", history)
    assert [(p["name"], p["status"]) for p in providers.store_listing()] == [("old", "deleted")]
    providers.store_listing()
    assert len(calls) == 1  # scanned once, not on every listing
    providers.restore_provider("old")
    assert providers.get_provider("old")["base_url"] == "https://old.test/v1"


def test_a_purge_is_not_undone_by_the_history_scan(monkeypatch):
    monkeypatch.setattr(provider_store, "_history_summaries",
                        lambda: ["providers.old: {'base_url': 'https://old.test/v1'} -> None"])
    providers.set_provider("old", "https://old.test/v1")
    providers.delete_provider("old")
    assert provider_store.purge("old") is True
    assert providers.store_listing() == []


# --------------------------------------------------------------- routes --
def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


AUTH = {"X-Dashboard-Token": "test-token"}


def test_routes_remove_list_restore_and_purge(monkeypatch, temp_db):
    client = _client(monkeypatch)
    assert client.post("/api/providers", json={"name": "acme", "base_url": "https://example.test/v1", "api_key": KEY}, headers=AUTH).status_code == 200

    removed = client.delete("/api/providers/acme", headers=AUTH)
    assert removed.status_code == 200 and removed.json()["restorable"] is True
    assert client.get("/api/providers", headers=AUTH).json()["providers"] == []

    listing = client.get("/api/providers/store?status=deleted", headers=AUTH).json()["providers"]
    assert [p["name"] for p in listing] == ["acme"] and listing[0]["has_key"] is True
    assert KEY not in client.get("/api/providers/store", headers=AUTH).text

    assert client.post("/api/providers/store/acme/restore", json={}, headers=AUTH).status_code == 200
    assert [p["name"] for p in client.get("/api/providers", headers=AUTH).json()["providers"]] == ["acme"]
    assert client.post("/api/providers/store/acme/restore", json={}, headers=AUTH).status_code == 409  # already back

    client.delete("/api/providers/acme", headers=AUTH)
    assert client.delete("/api/providers/store/acme", headers=AUTH).status_code == 200
    assert client.post("/api/providers/store/acme/restore", json={}, headers=AUTH).status_code == 404
    assert client.delete("/api/providers/store/acme", headers=AUTH).status_code == 404


def test_routes_require_the_token_and_validate_the_filter(monkeypatch, temp_db):
    client = _client(monkeypatch)
    assert client.get("/api/providers/store").status_code in (401, 403)
    assert client.post("/api/providers/store/x/restore", json={}).status_code in (401, 403)
    assert client.delete("/api/providers/store/x").status_code in (401, 403)
    assert client.get("/api/providers/store?status=bogus", headers=AUTH).status_code == 400
