"""ABP's DNS (bot/resolver.py): queries built and answers parsed in wire format, against a real DNS server on a
local socket (UDP, and TCP when the answer is truncated), DNS-over-HTTPS, the fallback order, and the
validated-only policy. The last tests ask Quad9 and a locally running Ironroot for real, when they can be reached."""
from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from bot import resolver as r


def _answer(query: bytes, records: list[tuple[int, bytes]], ad: bool = True, rcode: int = 0, tc: bool = False) -> bytes:
    """A real DNS answer to `query`: the question echoed, each record pointing at it by name compression."""
    qid = query[:2]
    i = 12
    while query[i] != 0:
        i += query[i] + 1
    question = query[12:i + 5]
    flags = 0x8180 | (0x0020 if ad else 0) | (0x0200 if tc else 0) | rcode
    out = qid + struct.pack(">HHHHH", flags, 1, len(records), 0, 0) + question
    for t, rdata in records:
        out += b"\xc0\x0c" + struct.pack(">HHIH", t, 1, 300, len(rdata)) + rdata
    return out


class LocalDNS:
    """A DNS server on 127.0.0.1 answering every question with the same records (truncating UDP when asked to)."""

    def __init__(self, records, ad=True, truncate_udp=False, rcode=0):
        self.records, self.ad, self.truncate, self.rcode = records, ad, truncate_udp, rcode
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.port = self.udp.getsockname()[1]
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", self.port))
        self.tcp.listen()
        self.seen: list[str] = []
        self.stop = False
        threading.Thread(target=self._serve_udp, daemon=True).start()
        threading.Thread(target=self._serve_tcp, daemon=True).start()

    def _serve_udp(self):
        self.udp.settimeout(0.2)
        while not self.stop:
            try:
                q, addr = self.udp.recvfrom(4096)
            except OSError:
                continue
            self.seen.append("udp")
            self.udp.sendto(_answer(q, [] if self.truncate else self.records, self.ad, self.rcode, tc=self.truncate), addr)

    def _serve_tcp(self):
        self.tcp.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.tcp.accept()
            except OSError:
                continue
            with conn:
                n = struct.unpack(">H", conn.recv(2))[0]
                q = conn.recv(n)
                self.seen.append("tcp")
                a = _answer(q, self.records, self.ad, self.rcode)
                conn.sendall(struct.pack(">H", len(a)) + a)

    def close(self):
        self.stop = True
        time.sleep(0.3)
        self.udp.close()
        self.tcp.close()


def _txt(s: str) -> bytes:
    return bytes([len(s)]) + s.encode()


def test_queries_ask_for_dnssec_and_answers_parse():
    q = r.build_query("example.com", "MX", 0x1234)
    assert q[:2] == b"\x12\x34" and q[2:4] == b"\x01\x20" and b"\x07example\x03com\x00" in q
    assert q[-10:-8] == struct.pack(">H", 41) and q[-4:-2] == b"\x80\x00"               # OPT with the DO bit
    mx = struct.pack(">H", 10) + b"\x04mail\xc0\x0c"
    a = r.parse_response(_answer(q, [(15, mx)]), "example.com", "MX", 0x1234)
    assert a.values == ["10 mail.example.com"] and a.validated and a.ttl == 300
    with pytest.raises(r.ResolveError, match="different question"):
        r.parse_response(_answer(q, []), "example.com", "MX", 0x9999)
    with pytest.raises(r.ResolveError, match="cannot look up"):
        r.build_query("example.com", "HINFO")
    aaaa = r.parse_response(_answer(q, [(28, bytes(15) + b"\x01")]), "x", "AAAA")
    assert aaaa.values == ["::1"]
    srv = r.parse_response(_answer(q, [(33, struct.pack(">HHH", 1, 2, 5269) + b"\x03xmp\xc0\x0c")]), "x", "SRV")
    assert srv.values == ["1 2 5269 xmp.example.com"]
    caa = r.parse_response(_answer(q, [(257, b"\x00\x05issue" + b"letsencrypt.org")]), "x", "CAA")
    assert caa.values == ['0 issue "letsencrypt.org"']
    assert r.parse_response(_answer(q, [(16, _txt("v=spf1 ") + _txt("-all"))]), "x", "TXT").values == ["v=spf1 -all"]


def test_udp_tcp_fallback_order_and_policy(monkeypatch):
    signed = LocalDNS([(1, bytes([192, 0, 2, 7]))], ad=True)
    big = LocalDNS([(1, bytes([192, 0, 2, 8]))], ad=True, truncate_udp=True)
    unsigned = LocalDNS([(1, bytes([192, 0, 2, 9]))], ad=False)
    failing = LocalDNS([], rcode=2)
    try:
        a = r.ask_dns(f"127.0.0.1:{signed.port}", "isc.org", "A")
        assert a.values == ["192.0.2.7"] and a.validated and a.via.startswith("ironroot@")
        assert r.ask_dns(f"127.0.0.1:{big.port}", "isc.org", "A").values == ["192.0.2.8"] and big.seen == ["udp", "tcp"]
        r.set_settings({"ironroot": f"127.0.0.1:{unsigned.port}", "upstreams": []})
        assert r.resolve("github.com").values == ["192.0.2.9"]                       # unsigned but answered
        with pytest.raises(r.ResolveError, match="without DNSSEC validation"):
            r.resolve("github.com", require_validated=True)
        r.set_settings({"ironroot": f"127.0.0.1:{failing.port}", "upstreams": []})
        with pytest.raises(r.ResolveError, match="no validated answer.*SERVFAIL"):
            r.resolve("dnssec-failed.org", require_validated=True)
        # SERVFAIL from the validator, not strict: the system resolver is the last resort (localhost always resolves)
        assert r.resolve("localhost").via == "system"
        # the first resolver is down: the next one answers
        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        r.set_settings({"ironroot": f"127.0.0.1:{dead_port}", "upstreams": ["quad9"], "timeout_s": 0.5})

        def doh(req: httpx.Request) -> httpx.Response:
            assert req.headers["content-type"] == "application/dns-message" and req.content[:2] == b"\x00\x00"
            return httpx.Response(200, content=_answer(req.content, [(1, bytes([9, 9, 9, 9]))]))
        got = r.resolve("quad9.net", transport=httpx.MockTransport(doh))
        assert got.values == ["9.9.9.9"] and got.via == "quad9" and got.validated
    finally:
        for s in (signed, big, unsigned, failing):
            s.close()


def test_settings_are_checked():
    with pytest.raises(ValueError, match="unknown resolver setting"):
        r.set_settings({"colour": 1})
    with pytest.raises(ValueError, match="an upstream is one of"):
        r.set_settings({"upstreams": ["8.8.8.8"]})
    assert r.set_settings({"upstreams": ["quad9", "https://dns.example/dns-query"]})["upstreams"][1].startswith("https://")


def _online() -> bool:
    try:
        socket.create_connection(("dns.quad9.net", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _online(), reason="Quad9 is not reachable from here")
def test_quad9_for_real():
    q = r.QUAD9["quad9"]
    signed = r.ask_doh(q, "isc.org", "A")
    assert signed.values and signed.validated                         # a signed zone: Quad9 validated it
    assert r.ask_doh(q, "dnssec-failed.org", "A").rcode == 2          # deliberately broken signatures: refused


IRONROOT = next((p for p in (Path("X:/cargo-target/release/ironroot.exe"), Path(shutil.which("ironroot") or "")) if p.is_file()), None)


@pytest.mark.skipif(IRONROOT is None or not _online(), reason="no ironroot build, or no network to resolve from the root")
def test_ironroot_for_real(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([str(IRONROOT), "--listen", f"127.0.0.1:{port}", "--trust-anchor", str(tmp_path / "root.key")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        a = None
        for _ in range(40):
            try:
                a = r.ask_dns(f"127.0.0.1:{port}", "isc.org", "A", timeout=5)
                break
            except (OSError, r.ResolveError):
                time.sleep(0.5)
        assert a is not None and a.values and a.validated              # resolved from the root down and validated
        assert r.ask_dns(f"127.0.0.1:{port}", "dnssec-failed.org", "A", timeout=10).rcode == 2
    finally:
        proc.kill()
        proc.wait()
