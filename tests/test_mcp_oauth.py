"""bot/agent_runtime/mcp_client.py's OAuth 2.1 harness — the pieces
around mcp.client.auth.OAuthClientProvider (dynamic client registration,
authorization code + PKCE) that BotServer itself owns: correlating the
dashboard's OAuth redirect-callback route back to the pending connect()
call that's waiting on it, and persisting tokens/client info against the
right external_mcp_servers row. `_DbTokenStorage` imports mcp.shared.auth
lazily, so this whole file needs the real `mcp` package (only in the
pipeline's bundled venv, not this dev shell — see test_mcp_client.py's
own docstring for the same situation), same as tests/test_mcp_sampling.py.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import db
from bot.agent_runtime import mcp_client


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_oauth_state():
    mcp_client._pending_oauth.clear()
    mcp_client._oauth_state_to_server.clear()
    yield
    mcp_client._pending_oauth.clear()
    mcp_client._oauth_state_to_server.clear()


def test_redirect_handler_records_the_pending_flow_and_state():
    handler = mcp_client._make_oauth_redirect_handler("my-server")

    _run(handler("https://auth.example.com/authorize?client_id=abc&state=xyz123&response_type=code"))

    assert mcp_client.oauth_authorization_url("my-server") == (
        "https://auth.example.com/authorize?client_id=abc&state=xyz123&response_type=code"
    )
    assert mcp_client._oauth_state_to_server["xyz123"] == "my-server"


def test_oauth_authorization_url_is_none_when_nothing_pending():
    assert mcp_client.oauth_authorization_url("no-such-server") is None


def test_deliver_oauth_callback_sets_the_event_and_returns_true():
    handler = mcp_client._make_oauth_redirect_handler("my-server")
    _run(handler("https://auth.example.com/authorize?state=xyz123"))
    pending = mcp_client._pending_oauth["my-server"]

    ok = mcp_client.deliver_oauth_callback("xyz123", "the-real-code")

    assert ok is True
    assert pending["code"] == "the-real-code"
    assert pending["state"] == "xyz123"
    assert pending["event"].is_set()


def test_deliver_oauth_callback_returns_false_for_an_unknown_state():
    assert mcp_client.deliver_oauth_callback("never-issued", "code") is False


def test_callback_handler_returns_the_delivered_code_and_state():
    handler = mcp_client._make_oauth_redirect_handler("my-server")
    _run(handler("https://auth.example.com/authorize?state=xyz123"))
    mcp_client.deliver_oauth_callback("xyz123", "the-real-code")

    callback_handler = mcp_client._make_oauth_callback_handler("my-server")
    result = _run(callback_handler())

    assert result.code == "the-real-code"
    assert result.state == "xyz123"


def test_callback_handler_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(mcp_client, "OAUTH_CALLBACK_TIMEOUT_S", 0.05)
    handler = mcp_client._make_oauth_redirect_handler("my-server")
    _run(handler("https://auth.example.com/authorize?state=xyz123"))  # never delivered

    callback_handler = mcp_client._make_oauth_callback_handler("my-server")

    with pytest.raises(TimeoutError, match="timed out waiting for OAuth"):
        _run(callback_handler())


def test_callback_handler_raises_if_no_flow_is_pending():
    callback_handler = mcp_client._make_oauth_callback_handler("never-started")

    with pytest.raises(RuntimeError, match="no pending OAuth"):
        _run(callback_handler())


def test_clear_pending_oauth_removes_both_the_flow_and_its_state_mapping():
    handler = mcp_client._make_oauth_redirect_handler("my-server")
    _run(handler("https://auth.example.com/authorize?state=xyz123"))

    mcp_client._clear_pending_oauth("my-server")

    assert mcp_client.oauth_authorization_url("my-server") is None
    assert "xyz123" not in mcp_client._oauth_state_to_server


def test_dashboard_oauth_redirect_uri_uses_dashboard_port(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PORT", "9999")

    assert mcp_client._dashboard_oauth_redirect_uri() == "http://127.0.0.1:9999/api/mcp-external/oauth/callback"


def test_db_token_storage_round_trip(temp_db):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    db.add_external_mcp_server("remote-one", "remote", url="https://example.com/mcp", oauth_enabled=True)
    storage = mcp_client._DbTokenStorage("remote-one")

    assert _run(storage.get_tokens()) is None
    assert _run(storage.get_client_info()) is None

    tokens = OAuthToken(access_token="tok123", token_type="Bearer")
    _run(storage.set_tokens(tokens))
    fetched_tokens = _run(storage.get_tokens())
    assert fetched_tokens.access_token == "tok123"

    client_info = OAuthClientInformationFull(
        client_id="client-abc", redirect_uris=["http://127.0.0.1:8787/api/mcp-external/oauth/callback"],
    )
    _run(storage.set_client_info(client_info))
    fetched_info = _run(storage.get_client_info())
    assert fetched_info.client_id == "client-abc"
