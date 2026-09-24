"""bot/peers.py's SSH auto-pairing: the peer-linking handshake also
exchanges and trusts SSH public keys and registers a working connection
back, automatically, with SSH Toolkit's own real functions (real
ssh-keygen, real file writes) isolated to a throwaway HOME via
ABP_SSH_TOOLKIT_HOME — same convention as tests/test_ssh_session_monitor.py.
No mocking of ssh_toolkit itself: this is what makes the feature real.
"""
from __future__ import annotations

import asyncio
import getpass
import json

import httpx
import pytest

from bot import db, peers, ssh_toolkit


@pytest.fixture
def ssh_toolkit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_SSH_TOOLKIT_HOME", str(tmp_path))
    return tmp_path


def test_safe_ssh_name_sanitizes_free_text():
    assert peers._safe_ssh_name("My Laptop!") == "abp-peer-my-laptop"
    assert peers._safe_ssh_name("") == "abp-peer-unnamed"
    assert peers._safe_ssh_name("  ") == "abp-peer-unnamed"


def test_setup_own_ssh_generates_a_real_keypair(temp_db, ssh_toolkit_home):
    async def _run():
        result = await peers._setup_own_ssh("Some Peer")
        assert result["connection_name"] == "abp-peer-some-peer"
        assert result["public_key"].startswith("ssh-ed25519 ")
        assert result["identity_file"]

        # Calling again reuses the same key rather than generating a new one.
        result2 = await peers._setup_own_ssh("Some Peer")
        assert result2["public_key"] == result["public_key"]

    asyncio.run(_run())


def test_accept_handshake_trusts_the_initiators_key_and_registers_a_connection(temp_db, ssh_toolkit_home):
    plaintext, _expires = db.create_server_pairing_token()
    fake_initiator_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKEYFORTESTFAKEKEYFORTEST0000 initiator@laptop"

    async def _run():
        return await peers.accept_handshake(
            name="Initiator", api_key="fake-inbound-credential", base_url="http://10.0.0.5:8787",
            my_name="Receiver", pairing_token=plaintext,
            ssh_public_key=fake_initiator_key, ssh_username="alice",
        )

    result = asyncio.run(_run())

    assert result["api_key"]
    assert result["name"] == "Receiver"
    assert result["ssh_public_key"].startswith("ssh-ed25519 ")
    assert result["ssh_username"] == getpass.getuser()

    peer_rows = db.list_peer_servers()
    assert any(r["name"] == "Initiator" for r in peer_rows)

    authorized_keys = ssh_toolkit_home / ".ssh" / "authorized_keys"
    assert authorized_keys.exists()
    assert fake_initiator_key in authorized_keys.read_text(encoding="utf-8")

    connections = asyncio.run(ssh_toolkit.list_connections())
    conn = next(c for c in connections if c["Name"] == "abp-peer-initiator")
    assert conn["HostName"] == "10.0.0.5"
    assert conn["User"] == "alice"


def test_accept_handshake_without_ssh_fields_still_succeeds(temp_db, ssh_toolkit_home):
    """A caller that never sent SSH info (an older client, or setup_ssh=False
    on their side) must not break the underlying API pairing — SSH
    auto-setup is additive, never load-bearing for the handshake itself."""
    plaintext, _expires = db.create_server_pairing_token()

    async def _run():
        return await peers.accept_handshake(
            name="Plain", api_key="fake-inbound-credential", base_url=None,
            my_name="Receiver", pairing_token=plaintext,
        )

    result = asyncio.run(_run())
    assert result["api_key"]
    # We still generate and offer our OWN key (useful even if they didn't send theirs)
    assert result["ssh_public_key"].startswith("ssh-ed25519 ")


def test_link_peer_exchanges_and_trusts_keys_both_ways(temp_db, ssh_toolkit_home, monkeypatch):
    """link_peer talks to a real (mocked-transport) remote server: the
    remote's own accept_handshake-shaped response carries ITS public key
    and OS username, and link_peer must trust that key locally and
    register a connection back to it — the initiating side's half of the
    same automatic pairing accept_handshake's tests cover for the receiver."""
    remote_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEREMOTEKEYFAKEREMOTEKEY000000 remote@server"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/peers/handshake"
        payload = json.loads(request.content)
        assert payload["ssh_public_key"].startswith("ssh-ed25519 ")
        assert payload["ssh_username"] == getpass.getuser()
        return httpx.Response(200, json={
            "api_key": "fake-outbound-credential", "name": "RemoteServer",
            "ssh_public_key": remote_key, "ssh_username": "bob",
        })

    mock_transport = httpx.MockTransport(handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = mock_transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(peers.httpx, "AsyncClient", _PatchedAsyncClient)

    pairing_token = peers._encode_pairing_token("http://10.0.0.9:8787", "unused-secret")

    async def _run():
        return await peers.link_peer("RemoteServer", pairing_token, "Initiator", setup_ssh=True)

    result = asyncio.run(_run())

    assert result["name"] == "RemoteServer"
    assert result["ssh_setup"]["trusted_remote_key"] is True
    assert result["ssh_setup"]["connection_registered"] is True

    authorized_keys = ssh_toolkit_home / ".ssh" / "authorized_keys"
    assert remote_key in authorized_keys.read_text(encoding="utf-8")

    connections = asyncio.run(ssh_toolkit.list_connections())
    conn = next(c for c in connections if c["Name"] == "abp-peer-remoteserver")
    assert conn["HostName"] == "10.0.0.9"
    assert conn["User"] == "bob"


def test_link_peer_with_setup_ssh_false_skips_all_ssh_work(temp_db, ssh_toolkit_home, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["ssh_public_key"] is None
        assert payload["ssh_username"] is None
        return httpx.Response(200, json={"api_key": "fake-outbound-credential", "name": "RemoteServer"})

    mock_transport = httpx.MockTransport(handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = mock_transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(peers.httpx, "AsyncClient", _PatchedAsyncClient)

    pairing_token = peers._encode_pairing_token("http://10.0.0.9:8787", "unused-secret")

    async def _run():
        return await peers.link_peer("RemoteServer", pairing_token, "Initiator", setup_ssh=False)

    result = asyncio.run(_run())

    assert "ssh_setup" not in result
    connections = asyncio.run(ssh_toolkit.list_connections())
    assert connections == []
