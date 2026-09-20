"""web_fetch and web_search: off by default, public-internet only, untrusted-labelled."""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from bot.agent_runtime import toolspec, tools, web
from bot.agent_runtime.errors import ToolError

PUBLIC = "93.184.216.34"
REAL_CLIENT = httpx.AsyncClient      # captured before any test patches it


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def enabled(monkeypatch):
    cfg = {"enabled": True}
    monkeypatch.setattr(web, "_config", lambda: cfg)
    return cfg


@pytest.fixture
def dns(monkeypatch):
    """Resolve names from a table; unknown names resolve to a public address."""
    table = {"internal.example": "10.0.0.5", "loop.example": "127.0.0.1", "meta.example": "169.254.169.254",
             "v6.example": "::1", "mapped.example": "::ffff:192.168.1.1"}

    def fake(host, port, *a, **k):
        ip = table.get(host, PUBLIC)
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


def serve(monkeypatch, handler):
    real = REAL_CLIENT
    monkeypatch.setattr(web, "_new_client", lambda: real(transport=httpx.MockTransport(handler), follow_redirects=False,
                                                        timeout=5))


def call(name, **inp):
    return run(tools.execute_tool(name, inp, workspace=tools.WORKSPACES_ROOT))


# ---- off by default ---------------------------------------------------------------
def test_web_tools_are_off_unless_enabled(monkeypatch):
    monkeypatch.setattr(web, "_config", lambda: {})
    names = {s["name"] for s in tools.all_tool_schemas()}
    assert "web_fetch" not in names and "web_search" not in names
    with pytest.raises(ToolError, match="unknown tool"):
        call("web_fetch", url="https://example.com")


def test_search_needs_a_provider_even_when_fetch_is_enabled(monkeypatch):
    monkeypatch.setattr(web, "_config", lambda: {"enabled": True})
    names = {s["name"] for s in tools.all_tool_schemas()}
    assert "web_fetch" in names and "web_search" not in names
    monkeypatch.setattr(web, "_config", lambda: {"enabled": True, "search": {"provider": "searxng", "base_url": "http://x"}})
    assert "web_search" in {s["name"] for s in tools.all_tool_schemas()}


def test_specs_are_read_only_network_tools():
    for n in ("web_fetch", "web_search"):
        spec = toolspec.spec_for(n)
        assert spec.permission == "network" and spec.read_only and spec.concurrency_safe


# ---- address checks -----------------------------------------------------------------
@pytest.mark.parametrize("ip,ok", [
    (PUBLIC, True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.0.1", False),
    ("169.254.169.254", False), ("0.0.0.0", False), ("224.0.0.1", False), ("::1", False), ("fe80::1", False),
    ("fc00::1", False), ("::ffff:10.0.0.1", False), ("not-an-ip", False),
])
def test_is_public_ip(ip, ok):
    assert web.is_public_ip(ip) is ok


@pytest.mark.parametrize("url,msg", [
    ("ftp://example.com/x", "only http and https"), ("file:///etc/passwd", "only http and https"),
    ("https://user:pw@example.com/", "embedded credentials"), ("http://localhost/", "not a public address"),
    ("http://printer.local/", "not a public address"), ("http:///path", "no host"),
    ("http://internal.example/", "public address"), ("http://loop.example/", "public address"),
    ("http://meta.example/latest/meta-data", "public address"), ("http://v6.example/", "public address"),
    ("http://mapped.example/", "public address"),
])
def test_unsafe_urls_are_refused(enabled, dns, url, msg):
    with pytest.raises(ToolError, match=msg):
        run(web.check_url(url))


def test_a_public_url_passes_and_deny_and_allow_lists_apply(enabled, dns):
    assert run(web.check_url("https://example.com/page")) == "https://example.com/page"
    enabled["deny_hosts"] = ["*.evil.test"]
    with pytest.raises(ToolError, match="deny list"):
        run(web.check_url("https://a.evil.test/"))
    enabled["allow_hosts"] = ["docs.python.org"]
    with pytest.raises(ToolError, match="allow list"):
        run(web.check_url("https://example.com/"))
    assert run(web.check_url("https://docs.python.org/3/"))


# ---- fetching -------------------------------------------------------------------------
PAGE = """<html><head><title>Hello &amp; welcome</title><style>p{color:red}</style><script>alert('x')</script></head>
<body><nav>Home | About</nav><main><h1>Main title</h1><p>First paragraph with a <a href="https://x.test/a">link</a>.</p>
<ul><li>one</li><li>two</li></ul><pre>  keep   spacing</pre></main><footer>copyright</footer></body></html>"""


def test_fetch_returns_readable_text_labelled_untrusted(enabled, dns, monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
    out = call("web_fetch", url="https://example.com/p")
    assert "UNTRUSTED CONTENT" in out and "HTTP 200" in out
    assert "Title: Hello & welcome" in out and "# Main title" in out
    assert "First paragraph with a link <https://x.test/a>." in out
    assert "- one" in out and "- two" in out and "keep   spacing" in out
    for junk in ("alert(", "color:red", "Home | About", "copyright"):
        assert junk not in out


def test_fetch_refuses_private_targets_before_connecting(enabled, dns, monkeypatch):
    hits = []
    serve(monkeypatch, lambda r: hits.append(str(r.url)) or httpx.Response(200, text="secret"))
    with pytest.raises(ToolError, match="public address"):
        call("web_fetch", url="http://internal.example/admin")
    assert hits == []


def test_a_redirect_to_a_private_address_is_refused(enabled, dns, monkeypatch):
    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="secret")

    serve(monkeypatch, handler)
    with pytest.raises(ToolError, match="public address"):
        call("web_fetch", url="https://example.com/")


def test_redirects_are_followed_within_a_limit(enabled, dns, monkeypatch):
    def handler(request):
        if request.url.path == "/end":
            return httpx.Response(200, text="arrived", headers={"content-type": "text/plain"})
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}" if n < 2 else "/end"})

    serve(monkeypatch, handler)
    assert "arrived" in call("web_fetch", url="https://example.com/0")
    serve(monkeypatch, lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(ToolError, match="too many redirects"):
        call("web_fetch", url="https://example.com/")


def test_errors_types_and_size_are_handled(enabled, dns, monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(404, text="nope", headers={"content-type": "text/plain"}))
    with pytest.raises(ToolError, match="HTTP 404"):
        call("web_fetch", url="https://example.com/x")
    serve(monkeypatch, lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    with pytest.raises(ToolError, match="cannot be shown as text"):
        call("web_fetch", url="https://example.com/x.png")
    big = "x" * (web.MAX_BODY_BYTES + 5000)
    serve(monkeypatch, lambda r: httpx.Response(200, text=big, headers={"content-type": "text/plain"}))
    out = call("web_fetch", url="https://example.com/big", max_chars=1000)
    assert "more characters" in out and "offset=1000" in out and len(out) < 2500
    part = call("web_fetch", url="https://example.com/big", max_chars=1000, offset=1000)
    assert "more characters" in part


def test_json_is_returned_as_is(enabled, dns, monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"}))
    assert '"a":1' in call("web_fetch", url="https://api.example.com/x").replace(" ", "")


def test_the_connected_peer_address_is_checked_too():
    class Stream:
        def __init__(self, addr):
            self.addr = addr

        def get_extra_info(self, name):
            return self.addr

    ok = httpx.Response(200, extensions={"network_stream": Stream((PUBLIC, 443))})
    bad = httpx.Response(200, extensions={"network_stream": Stream(("10.0.0.9", 443))})
    assert web._peer_is_public(ok) and not web._peer_is_public(bad)
    assert web._peer_is_public(httpx.Response(200))     # unknown peer: the earlier DNS check stands


# ---- search ---------------------------------------------------------------------------
def _search_cfg(**over):
    return {"enabled": True, "search": {"provider": "searxng", "base_url": "http://127.0.0.1:8080", **over}}


def _mock_search(monkeypatch, handler):
    real = REAL_CLIENT
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_searxng_results_are_formatted_and_labelled(monkeypatch):
    monkeypatch.setattr(web, "_config", lambda: _search_cfg())
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"results": [{"title": "T1", "url": "https://a.test", "content": "snippet one"},
                                                     {"title": "T2", "url": "https://b.test", "content": "two"}]})

    _mock_search(monkeypatch, handler)
    out = call("web_search", query="python asyncio", num=1)
    assert "UNTRUSTED CONTENT" in out and "1. T1" in out and "https://a.test" in out and "T2" not in out
    assert "format=json" in seen["url"] and "q=python+asyncio" in seen["url"]


def test_brave_and_tavily_use_their_keys_and_parse_their_shapes(monkeypatch):
    monkeypatch.setenv("SEARCH_KEY_FOR_TEST", "unused")
    cfg = {"enabled": True, "search": {"provider": "brave", "api_key_env": "SEARCH_KEY_FOR_TEST"}}
    monkeypatch.setattr(web, "_config", lambda: cfg)
    seen = {}

    def brave(request):
        seen["token"] = request.headers.get("x-subscription-token")
        return httpx.Response(200, json={"web": {"results": [{"title": "B", "url": "https://b.test", "description": "d"}]}})

    _mock_search(monkeypatch, brave)
    assert "https://b.test" in call("web_search", query="q") and seen["token"] == "unused"

    cfg["search"]["provider"] = "tavily"

    def tavily(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"results": [{"title": "Tv", "url": "https://t.test", "content": "c"}]})

    _mock_search(monkeypatch, tavily)
    assert "https://t.test" in call("web_search", query="q") and seen["auth"] == "Bearer unused"


def test_search_errors_are_explained(monkeypatch):
    cfg = {"enabled": True, "search": {"provider": "brave", "api_key_env": "NOT_SET_ANYWHERE_123"}}
    monkeypatch.setattr(web, "_config", lambda: cfg)
    monkeypatch.delenv("NOT_SET_ANYWHERE_123", raising=False)
    with pytest.raises(ToolError, match="NOT_SET_ANYWHERE_123"):
        call("web_search", query="q")
    monkeypatch.setattr(web, "_config", lambda: _search_cfg())
    _mock_search(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(ToolError, match="HTTP 503"):
        call("web_search", query="q")
    _mock_search(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    assert call("web_search", query="q") == "No results."
    with pytest.raises(ToolError, match="query is required"):
        call("web_search", query=" ")
