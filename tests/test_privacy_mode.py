"""Privacy mode (bot/privacy.py): with the switch on, only models on this machine answer and agent tools that reach
the network are refused; the router says why instead of quietly using the cloud."""
from __future__ import annotations

import asyncio

import pytest

from bot.backends.base import Backend, BackendError, BackendResult


class _Backend(Backend):
    def __init__(self, name, base_url=""):
        self.name, self.base_url, self.calls = name, base_url, 0

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.calls += 1
        return BackendResult(text=f"{self.name} answered")


def test_what_counts_as_local():
    from bot import privacy
    assert privacy.is_local_url("http://127.0.0.1:11436/v1") and privacy.is_local_url("http://localhost:11434")
    assert privacy.is_local_url("http://[::1]:8080") and privacy.is_local_url("http://models.localhost")
    assert not privacy.is_local_url("https://api.openai.com/v1") and not privacy.is_local_url("")
    assert not privacy.is_local_url("http://192.168.1.20:11434")
    assert privacy.is_local_url("http://192.168.1.20:11434", allow_lan=True)
    assert not privacy.is_local_url("http://8.8.8.8", allow_lan=True)
    assert not privacy.backend_is_local("api", _Backend("api", "http://127.0.0.1"))     # Claude is never local
    assert privacy.backend_is_local("custom_model", _Backend("custom_model", "http://127.0.0.1:11436/v1"))
    assert not privacy.backend_is_local("custom_model", _Backend("custom_model", "https://openrouter.ai/api/v1"))


def test_settings_persist_and_a_broken_file_fails_closed(tmp_path, monkeypatch):
    from bot import privacy
    assert privacy.settings() == {"enabled": False, "allow_lan": False}
    assert privacy.set_settings(enabled=True)["enabled"] and privacy.enabled()
    assert privacy.set_settings(allow_lan=True) == {"enabled": True, "allow_lan": True}
    p = tmp_path / "privacy.json"
    import os
    import time
    p.write_text("{broken")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert privacy.enabled()                                            # unreadable: on, never silently off


def test_the_router_only_uses_local_models_when_on(temp_db, monkeypatch):
    from bot import privacy, router as router_mod, setup_wizard
    cloud, local = _Backend("api"), _Backend("custom_model", "http://127.0.0.1:11436/v1")
    r = router_mod.Router()
    chain = {"names": ["api", "custom_model"]}
    monkeypatch.setattr(r, "resolve_chain", lambda *a, **k: chain["names"])
    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, **k: {"api": cloud, "custom_model": local}[name])
    monkeypatch.setattr(setup_wizard, "check_backend_ready", lambda name: (True, ""))
    assert asyncio.run(r.ask("hi")).text == "api answered"                 # off: the chain as configured
    privacy.set_settings(enabled=True)
    assert asyncio.run(r.ask("hi")).text == "custom_model answered" and cloud.calls == 1
    chain["names"] = ["api"]
    with pytest.raises(BackendError, match="privacy mode is on"):
        asyncio.run(r.ask("hi"))
    assert cloud.calls == 1                                             # the cloud model was never asked


def test_network_tools_are_refused_when_on(tmp_path):
    from bot import privacy
    from bot.agent_runtime import tools
    privacy.set_settings(enabled=True)
    for name in ("web_search", "browser_navigate", "consult_models", "dispatch_batch_completions"):
        assert "privacy mode is on" in privacy.check_tool(name)
    with pytest.raises(tools.ToolError, match="privacy mode is on"):
        asyncio.run(tools.execute_tool("consult_models", {"question": "x"}, workspace=tmp_path))
    (tmp_path / "w").mkdir()
    assert asyncio.run(tools.execute_tool("list_dir", {}, workspace=tmp_path / "w")) == "(empty)"   # local tools still work
    assert "not this machine" in privacy.check_tool("mcp_tool", "https://mcp.example.com/sse")
    assert privacy.check_tool("mcp_tool", "http://127.0.0.1:9000/mcp") is None
    privacy.set_settings(enabled=False)
    assert privacy.check_tool("web_search") is None


def test_the_privacy_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import privacy_api
    app = FastAPI()
    privacy_api.register(app, lambda: None)
    c = TestClient(app)
    assert c.get("/api/privacy").json()["enabled"] is False and "api" in c.get("/api/privacy").json()["cloud_backends"]
    assert c.put("/api/privacy", json={"enabled": True}).json() == {"enabled": True, "allow_lan": False}
    assert c.get("/api/privacy").json()["enabled"] is True
