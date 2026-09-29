"""A module hub that serves an OpenAI API (ModelMistress) shows up as a provider while it runs, and only then."""
from __future__ import annotations

import json
import os

import pytest

from bot import providers
from bot.modules import registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / m.id)
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    monkeypatch.setattr(providers, "file_providers", lambda: {"mine": {"base_url": "http://127.0.0.1:1/v1"}})
    (tmp_path / "modelmistress").mkdir()
    return tmp_path / "modelmistress"


def write_control(home, pid):
    (home / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:4321/", "token": "unused", "pid": pid,
                                                   "version": "0.1.0", "api": 1}), encoding="utf-8")


def test_a_running_hub_is_a_provider(home):
    write_control(home, os.getpid())
    p = providers.get_provider("modelmistress")
    assert p["base_url"] == "http://127.0.0.1:4321/v1"
    assert p["api_key"] == "unused" and p["module"] == "modelmistress"
    assert "mine" in providers.list_providers()


def test_no_hub_or_a_dead_one_is_no_provider(home, monkeypatch):
    assert providers.get_provider("modelmistress") is None
    write_control(home, 99999999)
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    monkeypatch.setattr(providers, "_pid_alive", lambda pid: False)
    assert providers.get_provider("modelmistress") is None


def test_a_provider_in_the_file_wins_and_the_store_never_sees_the_token(home, monkeypatch):
    write_control(home, os.getpid())
    monkeypatch.setattr(providers, "file_providers", lambda: {"modelmistress": {"base_url": "http://elsewhere/v1"}})
    assert providers.get_provider("modelmistress")["base_url"] == "http://elsewhere/v1"
    seen = {}
    from bot import provider_store
    monkeypatch.setattr(provider_store, "sync", lambda current: seen.update(current))
    monkeypatch.setattr(provider_store, "list_all", lambda status=None: [])
    monkeypatch.setattr(providers, "file_providers", lambda: {})
    providers.store_listing()
    assert "modelmistress" not in seen
