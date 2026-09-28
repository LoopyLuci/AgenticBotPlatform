"""Per-wire-protocol translation layer for bot/backends/native_backend.py's
one tool-calling loop — see base.py's module docstring."""

from __future__ import annotations

from typing import Optional

from bot.agent_runtime.transports.base import ProviderTransport


def build_openai_transport(
    *, protocol: str, base_url: str, api_key: Optional[str] = None, catalog_id: Optional[str] = None,
    name: Optional[str] = None,
) -> ProviderTransport:
    """The one place that picks between OpenAICompatibleTransport
    (chat-completions, the default) and ResponsesApiTransport
    (config/providers.yaml's `protocol: "responses"`) for a given named
    provider — every OpenAI-family call site (custom_model_backend.py,
    native_backend.py's fallback resolution, subagents.py, moa.py) goes
    through this instead of constructing a transport class directly, so
    a provider configured for the Responses API is honored consistently
    everywhere, not just wherever happened to be updated first."""
    if protocol == "responses":
        from bot.agent_runtime.transports.responses_api import ResponsesApiTransport

        transport = ResponsesApiTransport(base_url=base_url, api_key=api_key, catalog_id=catalog_id)
    else:
        from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport

        transport = OpenAICompatibleTransport(base_url=base_url, api_key=api_key, catalog_id=catalog_id)
    if name:
        # The providers.yaml name: the model router files each call's outcome under "<name>/<model>",
        # the same name it lists candidates by (bot/backends/native_backend.py's _ref).
        transport.router_provider = name
    return transport
