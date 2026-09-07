"""HermesGatewayBackend._session_params() — confirmed live against a real
running Hermes gateway (and its actual source,
tui_gateway/methods_session.py's session.create handler) that the RPC
wants `provider` and `model` as two separate params, not one combined
string. The original code sent only `{"model": self.model}`, so a value
like "nous/some-model-id" (this project's own established
"<provider>/<model_id>" convention) reached the gateway as a single
unresolvable model id with no provider at all, always failing with "No
inference provider configured" regardless of how valid the configured
model actually was.
"""

from __future__ import annotations

from bot.backends.hermes_gateway_backend import HermesGatewayBackend


def test_splits_provider_and_model_on_first_slash():
    backend = HermesGatewayBackend(model="nous/inclusionai/ling-3.0-flash-fin:free")

    assert backend._session_params() == {
        "provider": "nous",
        "model": "inclusionai/ling-3.0-flash-fin:free",
    }


def test_no_model_configured_sends_no_params():
    backend = HermesGatewayBackend(model=None)

    assert backend._session_params() == {}


def test_model_with_no_slash_passes_through_bare():
    # No provider prefix at all — nothing to split; pass through rather
    # than guessing a provider that was never given.
    backend = HermesGatewayBackend(model="some-bare-model-id")

    assert backend._session_params() == {"model": "some-bare-model-id"}
