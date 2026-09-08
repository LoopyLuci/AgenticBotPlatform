"""Router.ask()'s context["images"] handling when the resolved backend
chain has no vision-capable member (VISION_CAPABLE_BACKENDS) — an image
sent to a cli/ui/hermes_cli/hermes_gateway-backed instance must never
just silently vanish; it gets dropped with one honest note appended to
the prompt instead, mirroring native_backend.py's own treatment of an
image a transport can't handle.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.backends.base import BackendResult
from bot.config import config
from bot.router import Router


@pytest.fixture
def router_with_recording_backend(monkeypatch, temp_db):
    monkeypatch.setattr(config, "_data", {"default_backend": "api", "action_overrides": {}, "timeouts": {}})
    monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))

    r = Router()
    calls = []

    class _RecordingBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            calls.append({"prompt": prompt, "context": context or {}})
            return BackendResult(text="ok", tokens=None, raw=None)

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None, hermes_home=None: _RecordingBackend())
    return r, calls


def _ask(router, **kw):
    return asyncio.run(router.ask("what is this?", **kw))


def test_a_non_vision_backend_gets_the_image_dropped_with_a_note(router_with_recording_backend):
    router, calls = router_with_recording_backend
    images = [{"data": b"real bytes", "mime_type": "image/jpeg"}]

    _ask(router, backend_override="cli", context={"images": images})

    assert "could not be processed" in calls[0]["prompt"]
    assert "images" not in calls[0]["context"]


def test_a_vision_capable_backend_keeps_the_images_untouched(router_with_recording_backend):
    router, calls = router_with_recording_backend
    images = [{"data": b"real bytes", "mime_type": "image/jpeg"}]

    _ask(router, backend_override="api", context={"images": images})

    assert calls[0]["prompt"] == "what is this?"
    assert calls[0]["context"]["images"] == images


def test_no_images_is_unaffected(router_with_recording_backend):
    router, calls = router_with_recording_backend

    _ask(router, backend_override="cli")

    assert calls[0]["prompt"] == "what is this?"
    assert "images" not in calls[0]["context"]
