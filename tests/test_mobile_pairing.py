"""bot.mobile_pairing — the self-contained pairing code shared by the
dashboard's POST /api/mobile-keys route and the Support Bot's
mobile_key_create intent. detect_hosts() reuses the same detection
bot.network_info already has dedicated coverage for; this only needs to
prove the auto-fill order and the resulting code's shape/encoding.
"""

from __future__ import annotations

import asyncio

from bot import mobile_pairing


def _fake_addresses(monkeypatch, lan=None, tailscale=None, funnel=None):
    from bot import network_info

    monkeypatch.setattr(network_info, "detect_addresses", lambda: {"lan": lan, "tailscale": tailscale})
    monkeypatch.setattr(network_info, "detect_funnel_url", lambda: funnel)


def test_detect_hosts_fills_all_three_blanks_in_order(monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77", funnel="https://x.ts.net")

    host, host2, host3 = asyncio.run(mobile_pairing.detect_hosts())

    assert host == "192.168.1.50:8787"
    assert host2 == "100.101.98.77:8787"
    assert host3 == "https://x.ts.net"


def test_detect_hosts_leaves_explicit_values_untouched(monkeypatch):
    _fake_addresses(monkeypatch, lan="192.168.1.50", tailscale="100.101.98.77", funnel="https://x.ts.net")

    host, host2, host3 = asyncio.run(mobile_pairing.detect_hosts(host="manual.example:9999"))

    # The explicit host is never overwritten; detected candidates fill the
    # remaining blanks in order (LAN, Tailscale, Funnel) regardless of what
    # occupies the first slot.
    assert host == "manual.example:9999"
    assert host2 == "192.168.1.50:8787"
    assert host3 == "100.101.98.77:8787"


def test_detect_hosts_skips_detection_entirely_when_all_three_given(monkeypatch):
    from bot import network_info

    def _boom():
        raise AssertionError("should not be called when nothing is blank")

    monkeypatch.setattr(network_info, "detect_addresses", _boom)
    monkeypatch.setattr(network_info, "detect_funnel_url", _boom)

    host, host2, host3 = asyncio.run(mobile_pairing.detect_hosts("a:1", "b:2", "c:3"))

    assert (host, host2, host3) == ("a:1", "b:2", "c:3")


def test_build_pairing_code_embeds_all_three_hosts_and_the_key():
    code = mobile_pairing.build_pairing_code("secret-key", "192.168.1.50:8787", "100.101.98.77:8787", "https://x.ts.net")

    assert code.startswith("botserver://pair?")
    assert "host=192.168.1.50%3A8787" in code
    assert "host2=100.101.98.77%3A8787" in code
    assert "host3=https%3A%2F%2Fx.ts.net" in code
    assert "key=secret-key" in code


def test_build_pairing_code_omits_blank_slots():
    code = mobile_pairing.build_pairing_code("secret-key")

    assert code == "botserver://pair?key=secret-key"


def test_build_pairing_code_url_encodes_special_characters_in_the_key():
    code = mobile_pairing.build_pairing_code("abc/def+ghi=")

    assert "abc%2Fdef%2Bghi%3D" in code
