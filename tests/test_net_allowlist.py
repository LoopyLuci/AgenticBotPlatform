"""sandbox.network: allowlist - the filtering proxy's rules (local servers only, no internet), the
fail-closed refusals on every other backend, and, when a real Docker daemon and a local python
image exist, the whole thing against real containers on an internal network."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bot.agent_runtime import net_allowlist as na
from bot.agent_runtime import sandbox, tools
from bot.agent_runtime.errors import ToolError


# ---- patterns ---------------------------------------------------------------------------
def test_exact_wildcard_and_port_patterns():
    p = na.parse_pattern("PyPI.org.")
    assert p.matches("pypi.org", 80) and not p.matches("files.pypi.org", 80)
    w = na.parse_pattern("*.pythonhosted.org")
    assert w.matches("files.pythonhosted.org", 443) and w.matches("a.b.pythonhosted.org", 443)
    assert not w.matches("pythonhosted.org", 443) and not w.matches("evilpythonhosted.org", 443)
    ported = na.parse_pattern("api.example.com:443")
    assert ported.matches("api.example.com", 443) and not ported.matches("api.example.com", 80)
    v6 = na.parse_pattern("[2001:db8::1]:8080")
    assert v6.host == "2001:db8::1" and v6.port == 8080


@pytest.mark.parametrize("bad", ["", "*", "*.", "a*.example.com", "*.1.2.3.4", "host:0", "host:99999", "host:x", "a/b", "[::1"])
def test_bad_patterns_are_rejected(bad):
    with pytest.raises(ValueError):
        na.parse_pattern(bad)


def test_public_address_test():
    for ok in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
        assert na.is_public(ok)
    for bad in ("127.0.0.1", "::1", "10.1.2.3", "172.17.0.2", "192.168.0.9", "169.254.169.254", "100.64.0.1",
                "0.0.0.0", "224.0.0.1", "fe80::1", "::ffff:127.0.0.1", "fc00::1"):
        assert not na.is_public(bad), bad


# ---- decisions --------------------------------------------------------------------------
def fake_resolver(table):
    async def resolve(host, port):
        if host not in table:
            raise OSError("no such host")
        return table[host]
    return resolve


def decide(policy, host, port=443):
    return asyncio.run(policy.decide(host, port))


def test_only_listed_hosts_pass_and_they_resolve_to_public_addresses():
    pol = na.Policy(["pypi.org", "*.example.com:443"], resolver=fake_resolver(
        {"pypi.org": ["151.101.0.223"], "a.example.com": ["93.184.216.34"], "other.org": ["93.184.216.34"]}))
    assert decide(pol, "pypi.org").ok
    assert decide(pol, "PYPI.org.").ok
    assert decide(pol, "a.example.com", 443).ok
    assert decide(pol, "a.example.com", 80).reason == "not on the allow list"
    assert decide(pol, "other.org").reason == "not on the allow list"
    assert decide(pol, "example.com").reason == "not on the allow list"


def test_ip_literals_are_refused_unless_listed():
    pol = na.Policy(["pypi.org", "93.184.216.34"], resolver=fake_resolver({"pypi.org": ["151.101.0.223"]}))
    assert not decide(pol, "151.101.0.223").ok                  # the address of an allowed name is not an allowed name
    assert decide(pol, "93.184.216.34").ok                      # listed, and public
    assert not decide(pol, "8.8.8.8").ok


def test_private_loopback_and_link_local_are_refused_even_for_a_listed_name():
    table = {"loop.example": ["127.0.0.1"], "lan.example": ["192.168.1.5"], "meta.example": ["169.254.169.254"],
             "mixed.example": ["93.184.216.34", "10.0.0.1"], "v6.example": ["::1"]}
    pol = na.Policy(list(table) + ["127.0.0.1", "10.0.0.5"], resolver=fake_resolver(table))
    for host in table:
        d = decide(pol, host)
        assert not d.ok and "not a public address" in d.reason, host
    assert not decide(pol, "127.0.0.1").ok                      # a listed literal that is not public
    assert not decide(pol, "10.0.0.5").ok
    assert not decide(pol, "169.254.169.254").ok                # not even asked for: not listed


def test_private_hosts_must_be_named_in_the_private_list_and_that_also_allows_them():
    pol = na.Policy(["web.example"], ["nas.lan", "10.0.0.5:8080"],
                    resolver=fake_resolver({"nas.lan": ["192.168.1.9"], "web.example": ["192.168.1.9"]}))
    assert decide(pol, "nas.lan", 80).ok                        # not in allow_hosts, but private-listed
    assert decide(pol, "10.0.0.5", 8080).ok
    assert not decide(pol, "10.0.0.5", 80).ok                   # port is part of the entry
    assert not decide(pol, "web.example", 80).ok                # listed only as a public host, resolves privately


def test_unresolvable_names_and_bad_destinations_are_refused():
    pol = na.Policy(["gone.example"], resolver=fake_resolver({}))
    assert decide(pol, "gone.example").reason == "the name did not resolve"
    assert not decide(pol, "", 80).ok
    assert not decide(pol, "gone.example", 0).ok


# ---- the proxy, against local servers ----------------------------------------------------
class _Target(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"hello {self.path} via {self.headers.get('Host')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def target():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def raw(proxy_port, payload: bytes, *, read_until_close=True) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as s:
        s.sendall(payload)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out


class RunningProxy:
    def __init__(self, policy, **kw):
        self.log: list[dict] = []
        self.proxy = na.Proxy(policy, self.log.append, **kw)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.proxy.start("127.0.0.1", 0), self.loop).result(10)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.proxy.close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    @property
    def port(self):
        return self.proxy.port


def test_plain_http_to_a_private_listed_host_is_forwarded_and_logged(target):
    with RunningProxy(na.Policy([], ["localhost"])) as rp:
        out = raw(rp.port, f"GET http://localhost:{target}/x?y=1 HTTP/1.1\r\nHost: localhost:{target}\r\n"
                           f"Proxy-Connection: keep-alive\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200")
    assert b"hello /x?y=1 via localhost" in out
    assert rp.log[-1]["decision"] == "allow" and rp.log[-1]["host"] == "localhost"


def test_loopback_is_refused_when_only_publicly_listed(target):
    with RunningProxy(na.Policy(["localhost", "127.0.0.1"])) as rp:
        for host in ("localhost", "127.0.0.1"):
            out = raw(rp.port, f"GET http://{host}:{target}/ HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            assert out.startswith(b"HTTP/1.1 403") and b"not a public address" in out
    assert [r["decision"] for r in rp.log] == ["deny", "deny"]


def test_unlisted_host_is_refused_and_logged(target):
    with RunningProxy(na.Policy(["only.example"])) as rp:
        out = raw(rp.port, f"GET http://localhost:{target}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403") and b"not on the allow list" in out
    assert rp.log[0]["decision"] == "deny" and rp.log[0]["reason"] == "not on the allow list"


def test_connect_tunnels_bytes_for_a_listed_private_host(target):
    with RunningProxy(na.Policy([], [f"localhost:{target}"])) as rp:
        with socket.create_connection(("127.0.0.1", rp.port), timeout=10) as s:
            s.sendall(f"CONNECT localhost:{target} HTTP/1.1\r\nHost: localhost:{target}\r\n\r\n".encode())
            head = s.recv(4096)
            assert head.startswith(b"HTTP/1.1 200")
            s.sendall(b"GET /tunnel HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := s.recv(4096):
                data += chunk
    assert b"hello /tunnel" in data


def test_connect_to_an_unlisted_port_and_odd_requests_are_refused(target):
    with RunningProxy(na.Policy([], [f"localhost:{target}"])) as rp:
        assert raw(rp.port, f"CONNECT localhost:{target + 1} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
        assert raw(rp.port, b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert raw(rp.port, b"GET ftp://localhost/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert raw(rp.port, b"nonsense\r\n\r\n").startswith(b"HTTP/1.1 400")
        assert raw(rp.port, b"CONNECT nohostport HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")


def test_oversized_headers_are_refused(target):
    with RunningProxy(na.Policy([], ["localhost"])) as rp:
        junk = b"X-Junk: " + b"a" * (na.MAX_HEADER_BYTES * 2) + b"\r\n"
        out = raw(rp.port, f"GET http://localhost:{target}/ HTTP/1.1\r\n".encode() + junk + b"\r\n")
    assert out == b"" or out.startswith(b"HTTP/1.1 400")


def test_an_idle_tunnel_is_cut_after_the_idle_limit(target):
    with RunningProxy(na.Policy([], ["localhost"]), idle_timeout=0.5) as rp:
        with socket.create_connection(("127.0.0.1", rp.port), timeout=10) as s:
            s.sendall(f"CONNECT localhost:{target} HTTP/1.1\r\n\r\n".encode())
            assert s.recv(4096).startswith(b"HTTP/1.1 200")
            started = time.monotonic()
            assert s.recv(4096) == b""                           # the proxy hung up
            assert time.monotonic() - started < 5


def test_a_failing_log_callback_does_not_break_the_proxy(target):
    def boom(row):
        raise RuntimeError("log full")

    with RunningProxy(na.Policy([], ["localhost"])) as rp:
        rp.proxy._log = boom
        out = raw(rp.port, f"GET http://localhost:{target}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert b"hello /" in out


# ---- the sandbox side: fail closed ---------------------------------------------------------
def run_start(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_config", lambda: cfg)
    return asyncio.run(sandbox.start("echo hi", tmp_path, tmp_path))


@pytest.mark.parametrize("backend", ["local", "windows_job", "ssh", "wsl"])
def test_every_backend_but_docker_refuses_allowlist(backend, monkeypatch, tmp_path):
    cfg = {"backend": backend, "network": "allowlist", "allow_hosts": ["pypi.org"]}
    monkeypatch.setattr(sandbox.win_job, "is_supported", lambda: True)
    with pytest.raises(ToolError, match="only the docker backend"):
        run_start(cfg, monkeypatch, tmp_path)


def test_a_persistent_shell_is_not_offered_under_allowlist(monkeypatch):
    from bot.agent_runtime import persistent_shell

    monkeypatch.setattr(sandbox, "_config", lambda: {"backend": "local", "network": "allowlist"})
    assert "allowlist" in (persistent_shell.unsupported_reason() or "")
    with pytest.raises(ToolError):
        asyncio.run(sandbox.start_persistent(["sh"], persistent_shell.Path(".")))


def test_bad_host_patterns_and_network_overrides_stop_the_command_before_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "docker-that-must-not-run")
    with pytest.raises(ToolError, match="allow_hosts"):
        run_start({"backend": "docker", "network": "allowlist", "allow_hosts": ["*"]}, monkeypatch, tmp_path)
    with pytest.raises(ToolError, match="extra_args"):
        run_start({"backend": "docker", "network": "allowlist", "allow_hosts": ["a.example"],
                   "docker": {"extra_args": ["--network=host"]}}, monkeypatch, tmp_path)


def test_docker_argv_joins_the_internal_network_and_never_a_bridge(tmp_path):
    cfg = {"backend": "docker", "network": "allowlist", "docker": {"network": "bridge"}}
    argv = sandbox._docker_argv("true", tmp_path, tmp_path, "c1", cfg, [], attach="nal-net-x",
                                fixed_env={"HTTP_PROXY": "http://abp-proxy:3128"})
    assert argv[argv.index("--network") + 1] == "nal-net-x"
    assert "HTTP_PROXY=http://abp-proxy:3128" in argv


def test_a_proxy_that_cannot_start_means_the_command_does_not_run(monkeypatch, tmp_path):
    calls = []

    def fake_cli(docker, *args, timeout=60):
        calls.append(args[:2])
        code = 1 if args[:2] == ("network", "create") else 0
        return subprocess.CompletedProcess(args, code, "", "daemon says no")

    monkeypatch.setattr(sandbox, "_docker_cli", fake_cli)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "docker")
    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the command must not start")))
    with pytest.raises(ToolError, match="internal network"):
        run_start({"backend": "docker", "network": "allowlist", "allow_hosts": ["a.example"]}, monkeypatch, tmp_path)
    assert ("network", "rm") in calls                           # and what was made is removed again


def test_schema_offers_allowlist_and_its_two_lists():
    from bot.agent_runtime import settings_schema

    ids = {f["id"]: f for f in settings_schema.FIELDS}
    assert "allowlist" in [c[0] for c in ids["native_agent.sandbox.network"]["choices"]]
    assert ids["native_agent.sandbox.allow_hosts"]["type"] == "list"
    assert ids["native_agent.sandbox.allow_private_hosts"]["type"] == "list"


def test_proxy_log_lines_reach_the_trace_and_the_event_ring():
    from bot.sandbox_ns.registry import registry

    notes = []

    class T:
        def note(self, message, level="info"):
            notes.append((level, message))

    from bot.agent_runtime import trace

    real = trace.current
    trace.current = lambda: T()
    try:
        text = "\n".join([json.dumps({"event": "listening"}),
                          json.dumps({"decision": "deny", "method": "CONNECT", "host": "evil.example", "port": 443, "reason": "not on the allow list"}),
                          "not json"])
        sandbox._record_proxy_log(text, "curl evil.example")
    finally:
        trace.current = real
    assert notes == [("warn", "net deny CONNECT evil.example:443 (not on the allow list)")]
    assert any(e["kind"] == "net.deny" and "evil.example" in e["detail"] for e in registry.events(20))


# ---- live Docker --------------------------------------------------------------------------
def _docker_ready() -> str:
    docker = shutil.which("docker")
    if not docker:
        return ""
    try:
        if subprocess.run([docker, "version"], capture_output=True, timeout=15).returncode != 0:
            return ""
        for image in ("python:3.11-slim", "python:3.12-alpine"):
            if subprocess.run([docker, "image", "inspect", image], capture_output=True, timeout=15).returncode == 0:
                return image
    except Exception:
        return ""
    return ""


LIVE_IMAGE = _docker_ready()
live = pytest.mark.skipif(not LIVE_IMAGE, reason="no Docker daemon with a local python image (nothing is pulled)")


@pytest.fixture
def one_live_test_at_a_time():
    """The live tests assert that nothing of the sandbox is left over, which another worker's
    running test would make untrue; a lock file takes turns across xdist workers."""
    import os
    import tempfile

    lock = os.path.join(tempfile.gettempdir(), "abp-test-netallow-live.lock")
    deadline = time.monotonic() + 600
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 900:      # left by a crashed run
                    os.unlink(lock)
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("another live allowlist test held the lock for too long")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


@pytest.fixture
def live_net(one_live_test_at_a_time, monkeypatch, tmp_path):
    """An 'allowed' web server in a local container on the default bridge, reachable by the proxy
    (which has a leg there) and not by the command (which only has the internal network). Labelled
    and removed at the end, with anything the sandbox left behind."""
    docker = shutil.which("docker")
    name = f"netallow-test-target-{int(time.time())}"
    label = "abp.test.netallow=1"
    subprocess.run([docker, "run", "-d", "--name", name, "--label", label, "--network", "bridge", LIVE_IMAGE, "python", "-m", "http.server", "8000"],
                   capture_output=True, text=True, timeout=60, check=True)
    try:
        for _ in range(40):
            r = subprocess.run([docker, "logs", name], capture_output=True, text=True)
            if "Serving HTTP" in (r.stdout + r.stderr):
                break
            time.sleep(0.25)
        # an address the proxy can reach, and the name it will be asked for
        ip = subprocess.run([docker, "inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", name],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        cfg = {"backend": "docker", "network": "allowlist", "allow_hosts": [], "allow_private_hosts": [f"{ip}:8000"],
               "docker": {"image": LIVE_IMAGE}}
        monkeypatch.setattr(sandbox, "_config", lambda: cfg)
        ws = tmp_path / "ws"
        ws.mkdir()
        yield cfg, ip, ws.resolve()
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60)
        names, nets = _leftovers()
        for leftover in names:
            subprocess.run([docker, "rm", "-f", leftover], capture_output=True, timeout=60)
        for leftover in nets:
            subprocess.run([docker, "network", "rm", leftover], capture_output=True, timeout=60)


def _fetch(url_host_port, *, direct=False):
    host, port = url_host_port
    return ("import urllib.request,socket,sys\n"
            + ("urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))\n" if direct else "")
            + f"try:\n r=urllib.request.urlopen('http://{host}:{port}/', timeout=15); print('GOT', r.status)\n"
            "except Exception as e:\n print('FAILED', type(e).__name__, str(e)[:80])\n")


def _run(ws, code):
    return asyncio.run(tools.execute_tool("run_shell", {"command": f"python - <<'PYEND'\n{code}PYEND", "timeout": 120},
                                          workspace=ws))


def _leftovers():
    docker = shutil.which("docker")
    names = subprocess.run([docker, "ps", "-a", "--filter", "name=nal-proxy-", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=60).stdout.split()
    nets = subprocess.run([docker, "network", "ls", "--filter", "label=abp.netallow=1", "--format", "{{.Name}}"],
                          capture_output=True, text=True, timeout=60).stdout.split()
    return names, nets


@live
def test_live_allowed_host_is_reachable_through_the_proxy(live_net):
    cfg, ip, ws = live_net
    out = _run(ws, _fetch((ip, 8000)))
    assert "GOT 200" in out, out
    assert _leftovers() == ([], []), "the proxy container and the internal network are removed after the command"


@live
def test_live_a_host_not_on_the_list_is_refused_by_the_proxy(live_net):
    cfg, ip, ws = live_net
    cfg["allow_private_hosts"] = []
    cfg["allow_hosts"] = ["somewhere.example"]
    out = _run(ws, _fetch((ip, 8000)))
    assert "GOT" not in out and "FAILED" in out and "403" in out, out


@live
def test_live_a_listed_but_private_host_is_refused_without_the_private_list(live_net):
    cfg, ip, ws = live_net
    cfg["allow_private_hosts"] = []
    cfg["allow_hosts"] = [f"{ip}:8000"]
    out = _run(ws, _fetch((ip, 8000)))
    assert "GOT" not in out and "403" in out, out


@live
def test_live_a_direct_connection_without_the_proxy_has_no_route(live_net):
    cfg, ip, ws = live_net
    out = _run(ws, _fetch((ip, 8000), direct=True))
    assert "GOT" not in out and "FAILED" in out, out
    out = _run(ws, "import socket\ns=socket.socket(); s.settimeout(5)\n"
                   "try:\n s.connect(('1.1.1.1', 443)); print('CONNECTED')\nexcept Exception as e:\n print('BLOCKED', type(e).__name__)\n")
    assert "BLOCKED" in out and "CONNECTED" not in out, out
    assert _leftovers() == ([], [])


@live
def test_live_a_proxy_that_cannot_run_stops_the_command(live_net, tmp_path):
    cfg, ip, ws = live_net
    cfg["docker"] = {"image": LIVE_IMAGE, "proxy_image": "abp-no-such-image:0"}
    with pytest.raises(ToolError, match="proxy"):
        asyncio.run(sandbox.start("echo should-not-run", ws, ws))
    assert _leftovers() == ([], [])
