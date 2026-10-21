"""`ollama.enabled: false` (Settings → Local models): Ollama is neither loaded, offered nor used, and nothing is removed."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot import model_router, ollama
from bot.runtime import legacy, live

OFF = {"ollama": {"enabled": False}}
STATIC = Path(__file__).resolve().parents[1] / "bot" / "dashboard" / "static"


@pytest.fixture
def switched_off(monkeypatch):
    monkeypatch.setattr(ollama, "enabled", lambda cfg=None: False if cfg is None else (cfg.get("ollama") or {}).get("enabled") is not False)


def test_enabled_unless_the_config_says_false():
    assert ollama.enabled({}) and ollama.enabled({"ollama": {}}) and ollama.enabled({"ollama": {"enabled": True}})
    assert not ollama.enabled(OFF)
    assert ollama.is_ollama_ref("ollama/qwen3-vl:8b") and ollama.is_ollama_ref("my_ollama/llama3.1")
    assert not ollama.is_ollama_ref("modelmistress/qwen3") and not ollama.is_ollama_ref("unsloth/x")


def test_the_ollama_unit_is_not_loaded_when_switched_off(monkeypatch):
    monkeypatch.delenv("ABP_UNITS", raising=False)
    assert "ollama" not in live.selected_units(OFF)
    assert set(live.selected_units(OFF)) == set(legacy.AREAS) - {"ollama"}            # every other area still loads
    assert live.selected_units({"ollama": {"enabled": False}, "runtime": {"units": ["ollama", "vm-harness"]}}) == ["vm-harness"]
    assert "ollama" in live.selected_units({})                                          # the default is unchanged


def test_the_router_leaves_ollama_models_out(monkeypatch, switched_off):
    from bot import model_catalog, providers
    from bot.modelmistress import client as mm

    monkeypatch.setattr(mm, "replaced_by_modelmistress", lambda name, pcfg=None: False)
    monkeypatch.setattr(providers, "list_providers", lambda: {"ollama": {}, "my-router": {}})
    monkeypatch.setattr(model_catalog, "catalog_provider_for", lambda name, base_url="": "openrouter" if name == "my-router" else name)
    monkeypatch.setattr(model_catalog, "search", lambda **kw: [{"provider": "openrouter", "model": "qwen/qwen3-8b:free"}]
                        if kw.get("provider") == "my-router" else [])
    monkeypatch.setattr(model_router, "_local_models", lambda name, pcfg: ["local"] if name == "ollama" else None)
    monkeypatch.setattr(model_router, "_cfg", lambda: {"also": ["ollama/qwen3-vl:8b", "nous/x:free"]})
    assert model_router.candidate_models() == ["my-router/qwen/qwen3-8b:free", "nous/x:free"]
    monkeypatch.setattr(model_router, "_cfg", lambda: {"candidates": ["ollama/local", "modelmistress/coder"]})
    assert model_router.candidate_models() == ["modelmistress/coder"]                    # even from an explicit list


def test_find_reports_no_ollama_when_switched_off(monkeypatch, switched_off):
    from bot.ollama import client

    monkeypatch.setattr(client, "_discover", lambda: pytest.fail("a switched-off Ollama is never looked for"))
    assert client.find(refresh=True) is None
    assert client.find_cached() is None


def test_discovery_skips_the_ollama_store(switched_off):
    from bot.localai import discover

    assert all(loc["app"] != "Ollama" for loc in discover.known_locations())


def test_local_chat_leaves_out_ollama_pass_through_models(switched_off):
    from bot.localchat import catalog

    rows = [{"id": "ollama/tutor:latest", "source": "ollama", "kind": "chat"}, {"id": "unsloth/coder", "kind": "chat"}]

    def call(op, args):
        return {"model.list": rows, "service.status": {}, "model.loaded": [], "model.plan": None}.get(op)

    ids = [m["id"] for m in catalog.build(call, reports=[], min_context=0)["models"]]
    assert "ollama/tutor:latest" not in ids and "unsloth/coder" in ids


def test_settings_has_the_switch_in_both_uis():
    js = (STATIC / "settings-hub.js").read_text(encoding="utf-8")
    assert 'data-sh-switch="ollama"' in js and "['ollama', 'enabled']" in js and "/api/runtime/units/ollama/" in js
    desktop = STATIC.parents[2] / "desktop-app" / "ui" / "settings-hub.js"
    assert desktop.read_text(encoding="utf-8") == js
