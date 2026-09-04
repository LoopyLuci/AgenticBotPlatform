"""bot.network_info.detect_addresses() — LAN/Tailscale address detection
used to auto-fill a mobile pairing key's host/host2 fields. The LAN
address comes from a real UDP "connect" trick (asks the OS routing table
to pick an address, no packets sent) mirroring the desktop app's own Rust
implementation; Tailscale comes from scanning interface addresses for the
CGNAT range. Both entry points are faked directly here rather than
depending on this machine's actual network state, so results are
deterministic regardless of what the test runner's own interfaces look
like.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from bot import network_info


@dataclass
class _FakeAddr:
    family: int
    address: str


def _patch_interfaces(monkeypatch, interfaces: dict[str, list[_FakeAddr]]):
    import psutil

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: interfaces)


def _patch_default_route(monkeypatch, ip):
    monkeypatch.setattr(network_info, "_default_route_lan_ip", lambda: ip)


def test_detects_lan_via_default_route_and_tailscale_via_interfaces(monkeypatch):
    _patch_default_route(monkeypatch, "192.168.1.50")
    _patch_interfaces(monkeypatch, {
        "Tailscale": [_FakeAddr(socket.AF_INET, "100.101.98.77")],
    })

    result = network_info.detect_addresses()

    assert result == {"lan": "192.168.1.50", "tailscale": "100.101.98.77"}


def test_ignores_loopback_and_link_local(monkeypatch):
    _patch_default_route(monkeypatch, "127.0.0.1")
    _patch_interfaces(monkeypatch, {
        "auto": [_FakeAddr(socket.AF_INET, "169.254.1.2")],
    })

    result = network_info.detect_addresses()

    assert result == {"lan": None, "tailscale": None}


def test_ignores_public_default_route(monkeypatch):
    # A machine with no private default route (e.g. a bare cloud VM) must
    # not report a public IP as "lan" — that's not a private/host-reachable
    # address in the sense this module promises.
    _patch_default_route(monkeypatch, "8.8.8.8")
    _patch_interfaces(monkeypatch, {})

    result = network_info.detect_addresses()

    assert result["lan"] is None


def test_default_route_lookup_failing_still_finds_tailscale(monkeypatch):
    _patch_default_route(monkeypatch, None)
    _patch_interfaces(monkeypatch, {
        "Tailscale": [_FakeAddr(socket.AF_INET, "100.101.98.77")],
    })

    result = network_info.detect_addresses()

    assert result == {"lan": None, "tailscale": "100.101.98.77"}


def test_falls_back_to_interface_scan_when_default_route_has_no_lan_match(monkeypatch):
    # No usable default route (e.g. offline, or it resolved to something
    # not in a private range) — still find a LAN address via interfaces if
    # one exists, rather than giving up on "lan" entirely.
    _patch_default_route(monkeypatch, None)
    _patch_interfaces(monkeypatch, {
        "Ethernet": [_FakeAddr(socket.AF_INET, "10.0.0.5")],
    })

    result = network_info.detect_addresses()

    assert result["lan"] == "10.0.0.5"


def test_covers_all_three_private_lan_ranges(monkeypatch):
    _patch_default_route(monkeypatch, None)

    _patch_interfaces(monkeypatch, {"a": [_FakeAddr(socket.AF_INET, "10.0.0.5")]})
    assert network_info.detect_addresses()["lan"] == "10.0.0.5"

    _patch_interfaces(monkeypatch, {"b": [_FakeAddr(socket.AF_INET, "172.20.0.5")]})
    assert network_info.detect_addresses()["lan"] == "172.20.0.5"

    _patch_interfaces(monkeypatch, {"c": [_FakeAddr(socket.AF_INET, "192.168.5.5")]})
    assert network_info.detect_addresses()["lan"] == "192.168.5.5"


def test_no_interfaces_and_no_default_route_returns_none_for_both(monkeypatch):
    _patch_default_route(monkeypatch, None)
    _patch_interfaces(monkeypatch, {})

    assert network_info.detect_addresses() == {"lan": None, "tailscale": None}


def test_never_raises_when_psutil_explodes(monkeypatch):
    import psutil

    _patch_default_route(monkeypatch, "192.168.1.50")

    def _boom():
        raise OSError("no permission to enumerate interfaces")

    monkeypatch.setattr(psutil, "net_if_addrs", _boom)

    assert network_info.detect_addresses() == {"lan": "192.168.1.50", "tailscale": None}


def test_never_raises_when_default_route_lookup_explodes(monkeypatch):
    def _boom():
        raise OSError("network unreachable")

    monkeypatch.setattr(network_info, "_default_route_lan_ip", _boom)
    _patch_interfaces(monkeypatch, {})

    assert network_info.detect_addresses() == {"lan": None, "tailscale": None}
