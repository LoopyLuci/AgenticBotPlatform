"""Grok in the Agentic Workspace's model picker: a "cloud" group from `grok models`, picked explicitly, routed to the grok backend."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from bot.localchat import catalog, chat

_REAL_GROK_MODELS = catalog.grok_models          # taken at import, before conftest's _no_grok_sign_in replaces it
MODELS_OUT = "You are logged in with grok.com.\n\nDefault model: grok-4.7\n\nAvailable models:\n  * grok-4.7 (default)\n  - grok-4.7-build-fast\n"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(catalog, "_grok_cache", (0.0, []))
    monkeypatch.setattr(catalog, "grok_models", _REAL_GROK_MODELS)


def _fake_run(out):
    return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")


def test_grok_models_parses_the_signed_in_list(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/bin/grok")
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT))
    assert catalog.grok_models() == ["grok-4.7", "grok-4.7-build-fast"]


def test_not_signed_in_or_not_installed_offers_nothing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: "/bin/grok")
    monkeypatch.setattr(subprocess, "run", _fake_run("You are not authenticated.\n\nAvailable models:\n  * grok-4.6 (default)\n"))
    assert catalog.grok_models() == []
    monkeypatch.setattr("shutil.which", lambda b: None)
    assert catalog.grok_models() == []


def test_cloud_rows_join_the_catalog_in_their_own_group():
    rows = catalog.cloud_rows(["grok-4.7"])
    assert rows[0]["id"] == "grok/grok-4.7" and rows[0]["runs_on"] == "cloud" and rows[0]["kind"] == "chat"
    doc = catalog.with_cloud({"models": [{"id": "modelmistress/x"}], "groups": ["loaded"]}, rows)
    assert [m["id"] for m in doc["models"]] == ["modelmistress/x", "grok/grok-4.7"] and doc["groups"] == ["loaded", "cloud"]
    assert catalog.with_cloud({"models": [], "groups": []}, []) == {"models": [], "groups": []}


def test_a_grok_session_with_cloud_assist_off_is_refused(monkeypatch):
    session = {"id": "s1", "mode": "chat", "model": "grok/grok-4.7", "cloud_assist": "off"}
    monkeypatch.setattr(chat, "is_running", lambda sid: False)
    monkeypatch.setattr(chat.store, "get", lambda sid: session)
    assert chat.is_grok("grok/grok-4.7") and not chat.is_grok("modelmistress/qwen")
    with pytest.raises(ValueError, match="cloud assist is off"):
        asyncio.run(chat.send("s1", "hi", lambda *a, **k: None))


def test_the_workspace_reads_and_sets_the_factory_grok_mode(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from bot.conductor import grok_worker
    from bot.config import config
    from bot.dashboard.server import build_app

    gcfg = {"mode": "off"}
    written = []
    monkeypatch.setattr(grok_worker, "settings", lambda cfg=None: dict(gcfg))

    def set_values(changes, actor="dashboard"):
        written.append(dict(changes))
        gcfg.update({k[-1]: v for k, v in changes.items()})
    monkeypatch.setattr(config, "set_values", set_values)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "test-token"}

    d = c.get("/api/local-chat/grok-mode", headers=h).json()
    assert d["mode"] == "off" and d["modes"] == list(grok_worker.MODES) and d["effective"] == "off"
    d = c.put("/api/local-chat/grok-mode", headers=h, json={"mode": "confined"}).json()
    assert written[-1] == {("conductor", "grok", "mode"): "confined"}
    assert d["mode"] == "confined" and d["effective"] == "off" and "network" in d["why"]   # not approved: says why
    d = c.put("/api/local-chat/grok-mode", headers=h, json={"mode": "auto"}).json()
    assert d["effective"] == "advisor"                                                    # auto falls back, never stops
    assert c.put("/api/local-chat/grok-mode", headers=h, json={"mode": "yolo"}).status_code == 400
    assert len(written) == 2


def test_network_approval_switches_on_and_back_off_from_the_workspace(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from bot.conductor import grok_worker
    from bot.config import config
    from bot.dashboard.server import build_app

    gcfg = {"mode": "advisor"}
    written = []
    monkeypatch.setattr(grok_worker, "settings", lambda cfg=None: dict(gcfg))

    def set_values(changes, actor="dashboard"):
        written.append(dict(changes))
        gcfg.update({k[-1]: v for k, v in changes.items()})
    monkeypatch.setattr(config, "set_values", set_values)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "test-token"}

    assert c.put("/api/local-chat/grok-mode", headers=h, json={"network_approved": True}).json()["network_approved"] is True
    assert c.put("/api/local-chat/grok-mode", headers=h, json={"network_approved": False}).json()["network_approved"] is False
    assert written == [{("conductor", "grok", "network_approved"): True}, {("conductor", "grok", "network_approved"): False}]
    assert gcfg["mode"] == "advisor"                                                      # the other field is untouched
    assert c.put("/api/local-chat/grok-mode", headers=h, json={"network_approved": "yes"}).status_code == 400
    assert c.put("/api/local-chat/grok-mode", headers=h, json={}).status_code == 400


def test_grok_is_on_auto_by_default_and_auto_without_grok_is_the_plain_local_worker(monkeypatch):
    from bot import conductor
    from bot.conductor import grok_worker, run
    from bot.localchat import factory

    assert conductor.DEFAULTS["grok"]["mode"] == "auto" and conductor.DEFAULTS["grok"]["network_approved"] is False

    class Local:
        def __init__(self, *a, **k):
            pass
    monkeypatch.setattr(run, "AgentLoopWorker", Local)
    monkeypatch.setattr(grok_worker, "settings", lambda cfg=None: {"mode": "auto"})
    monkeypatch.setattr(grok_worker, "installed", lambda: False)
    assert isinstance(factory.make_worker("/ws/repo", "m"), Local)          # no Grok here: the run is never refused for it
    monkeypatch.setattr(grok_worker, "installed", lambda: True)
    monkeypatch.setattr(grok_worker, "resolve_mode", lambda gcfg: "advisor")
    assert isinstance(factory.make_worker("/ws/repo", "m"), grok_worker.GrokWorker)
