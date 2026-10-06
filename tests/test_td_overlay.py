"""ABP's nodes on TransferDaemon's overlay: pairing that pairs TransferDaemon too, peers reached only over the
overlay, route changes, placement on route metrics, a big result moved by TD's own file transfer, the absence
of any firewall call in the default flow, and the fallback when TransferDaemon is not installed at all.

The stand-in hub implements TD's overlay contract (overlay.status/peers/invite_create/invite_accept/
expose/unexpose/connect/disconnect/services/proxy_status/events and transfers.send_file) and answers over
httpx, exactly like tests/test_transferdaemon.py's FakeHub does. A test against a real TransferDaemon with the
overlay runs when one is built next to ABP, and skips with that reason when there is none.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bot import db, peers
from bot.cluster import membership, scheduler, store
from bot.transferdaemon import client, harness, overlay

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "abp"


class FakeOverlay:
    """A stand-in for TransferDaemon's overlay hub: the contract above, plus a file transfer that really moves
    the bytes (into a downloads folder, as the real daemon does), so a test can check what arrived."""

    def __init__(self, node_id: str = "td-self", tmp_path: Path | None = None):
        self.node_id = node_id
        self.calls: list[tuple[str, dict]] = []
        self.paired: dict[str, str] = {}                 # invite -> the node that accepted it
        self.exposed: dict[str, dict] = {}               # service -> what was published
        self.connects: dict[str, str] = {}               # node id -> the endpoint handed out
        self.route: dict = {"kind": "direct", "rtt_ms": 12.0, "loss": 0.0, "mbps": 900.0, "state": "up"}
        self.routes: list[dict] = []
        self.services: list[dict] = []
        self.group: dict[str, list[str]] = {}
        self.events: list[dict] = []
        self.proxy_status: dict = {"ok": True, "relays": 2}
        self.sent: list[dict] = []
        self.downloads = tmp_path / "downloads" if tmp_path else None

    # ---- the hub's HTTP surface -------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "pid": 4242, "version": "0.1.0"})
        if request.headers.get("Authorization") != "Bearer unused":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no token"}})
        if request.url.path == "/v1/operations":
            return httpx.Response(200, json=[{"id": op, "group": op.split(".")[0], "summary": op, "mutating": True,
                                             "destructive": False, "streaming": False,
                                             "input": {"type": "object"}, "output": {}}
                                            for op in OPS])
        body = json.loads(request.content or b"null")
        op = request.url.path.removeprefix("/v1/call/")
        self.calls.append((op, body if isinstance(body, dict) else {}))
        result = getattr(self, f"_op_{op.replace('.', '_')}", None)
        if result is None:
            return httpx.Response(404, json={"error": {"code": "unknown_operation", "message": f"no {op}"}})
        return httpx.Response(200, json={"result": result(body)})

    # ---- the operations ---------------------------------------------------------------------------
    def _op_overlay_status(self, _b):
        return {"node_id": self.node_id, "name": self.node_id, "state": "up", "services": [SERVICE]}

    def _op_overlay_peers(self, _b):
        return {"peers": [{"node_id": self.node_id, "name": "self", **self.route}]}

    def _op_overlay_invite_create(self, b):
        return {"invite": f"inv-{self.node_id}", "node_id": self.node_id, "ttl_s": b.get("ttl_s"),
                "expires_at": "2099-01-01T00:00:00+00:00"}

    def _op_overlay_invite_accept(self, b):
        invite = str(b.get("invite") or "")
        peer = invite.removeprefix("inv-") or "td-peer"
        self.paired[invite] = peer
        self.connects.setdefault(peer, f"http://127.0.0.1:19000/{peer}")
        self.routes.append({"node_id": peer, "name": b.get("name") or peer, **self.route})
        return {"node_id": peer, "name": b.get("name") or peer}

    def _op_overlay_expose(self, b):
        self.exposed[SERVICE] = {"local_addr": b.get("local_addr"), "allow": b.get("allow") or []}
        return {"service": SERVICE, "local_addr": b.get("local_addr"), "allow": b.get("allow") or []}

    def _op_overlay_unexpose(self, _b):
        self.exposed.pop(SERVICE, None)
        return {"service": SERVICE, "exposed": False}

    def _op_overlay_connect(self, b):
        target = str(b.get("node_id") or b.get("group") or "")
        if not target:
            raise AssertionError("overlay.connect needs node_id or group")
        if b.get("group"):
            members = self.group.get(target) or []
            if not members:
                return {}
            target = members[0]
        return {"local_addr": self.connects.get(target, f"http://127.0.0.1:19000/{target}"),
                "route": {"node_id": target, **self.route}}

    def _op_overlay_disconnect(self, b):
        self.connects.pop(str(b.get("node_id") or ""), None)
        return {"node_id": b.get("node_id"), "connected": False}

    def _op_overlay_services(self, _b):
        return {"services": [{"service": SERVICE, "group": g, "nodes": nodes}
                             for g, nodes in sorted(self.group.items())]}

    def _op_overlay_proxy_status(self, _b):
        return dict(self.proxy_status)

    def _op_overlay_events(self, _b):
        return {"events": list(self.events)}

    def _op_transfers_send_file(self, b):
        src = Path(str(b.get("file_path") or ""))
        self.sent.append({"contact_id": b.get("contact_id"), "file_path": str(src), "size": src.stat().st_size
                          if src.is_file() else 0})
        if self.downloads is not None and src.is_file():
            self.downloads.mkdir(parents=True, exist_ok=True)
            (self.downloads / src.name).write_bytes(src.read_bytes())      # the real daemon's own behaviour
        return {"id": f"t-{len(self.sent)}", "file_name": src.name, "status": "pending",
                "size_bytes": src.stat().st_size if src.is_file() else 0}

    def ops_called(self, prefix: str) -> list[dict]:
        return [b for op, b in self.calls if op.startswith(prefix)]


def _handshake_reply(body: dict, url: str) -> httpx.Response:
    """A real httpx.Response (with its request attached, so raise_for_status works) standing in for the other
    machine's /api/peers/handshake answer."""
    return httpx.Response(200, json=body, request=httpx.Request("POST", url))


OPS = ["overlay.status", "overlay.peers", "overlay.invite_create", "overlay.invite_accept", "overlay.expose",
       "overlay.unexpose", "overlay.connect", "overlay.disconnect", "overlay.services", "overlay.proxy_status",
       "overlay.events", "transfers.send_file"]


@pytest.fixture
def td(tmp_path, monkeypatch):
    """A stand-in overlay hub, wired in as this machine's TransferDaemon, with an empty data dir."""
    fake = FakeOverlay(tmp_path=tmp_path)
    monkeypatch.setenv("TRANSFERD_DATA_DIR", str(tmp_path / "td"))
    (tmp_path / "td" / "transferdaemon").mkdir(parents=True, exist_ok=True)
    (tmp_path / "td" / "transferdaemon" / "control.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9", "token": "unused", "pid": 4242}), encoding="utf-8")
    monkeypatch.setenv("TRANSFERD_DOWNLOADS_DIR", str(fake.downloads))
    monkeypatch.setattr(harness, "install_dir", lambda: tmp_path / "td-checkout")
    monkeypatch.setattr(harness, "_is_checkout", lambda p: False)
    monkeypatch.setattr(harness, "binary", lambda name: tmp_path / "no-such-binary")
    transport = httpx.MockTransport(fake.handle)
    real = httpx.Client

    def get(url, **kw):
        with real(transport=transport) as c:
            return c.get(url)

    def request(method, url, **kw):
        with real(transport=transport) as c:
            return c.request(method, url, json=kw.get("json"), headers=kw.get("headers"))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "request", request)
    client._ops.update(at=0.0, url="", ops=[])
    overlay.forget_cache()
    peers._routes.clear()
    yield fake
    overlay.forget_cache()
    peers._routes.clear()


@pytest.fixture
def peer(temp_db, monkeypatch):
    """A linked peer with a TransferDaemon node id and an unreachable advertised address — the behind-NAT case."""
    key_id, key = db.create_api_key("peer: laptop", kind="peer_server")
    pid = db.create_peer_server("Laptop", "http://10.0.2.15:8787", key, key_id)
    db.set_peer_transport(pid, td_node_id="td-laptop")
    peers._routes.clear()
    yield db.get_peer_server(pid)
    peers._routes.clear()


# ---- pairing pairs TransferDaemon too ---------------------------------------------------------------------------------
def test_pairing_carries_the_td_invite_and_pairs_the_daemons(temp_db, td):
    token = peers.generate_pairing_token("http://192.168.1.20:8787", "home")
    assert "overlay.invite_create" in [op for op, _ in td.calls], td.calls
    assert td.ops_called("overlay.invite_create")[0]["ttl_s"] == 600
    assert td.exposed[SERVICE] == {"local_addr": "http://192.168.1.20:8787", "allow": ["*paired"]}, td.exposed

    base_url, secret, carried = peers._decode_pairing_token(token["pairing_token"])
    assert base_url == "http://192.168.1.20:8787"
    assert carried["invite"] == "inv-td-self" and carried["node_id"] == "td-self"
    assert td.paired == {}                                             # issued here; the linking side accepts it
    assert db.consume_server_pairing_token(secret)                     # and it is a real single-use token
    assert token["transferdaemon"]["available"] is True
    assert token["transferdaemon"]["node_id"] == "td-self"


def test_an_old_pairing_token_still_decodes_and_carries_no_td(temp_db, monkeypatch):
    monkeypatch.setattr(peers, "td_identity", lambda *a, **k: {})
    old = "bsp1." + peers._b64(b"http://10.0.0.5:8787") + ".secret"
    base_url, secret, td_part = peers._decode_pairing_token(old)
    assert (base_url, secret, td_part) == ("http://10.0.0.5:8787", "secret", {})
    with pytest.raises(peers.PeerError, match="valid pairing token"):
        peers._decode_pairing_token("nonsense")


def test_linking_pairs_td_and_travels_over_the_overlay(temp_db, td, monkeypatch):
    """The handshake goes to the overlay endpoint, not to the advertised 10.0.2.15 that cannot answer."""
    seen: dict = {}

    async def fake_post(client, url, **kw):
        seen["url"] = url
        seen["payload"] = kw.get("json")
        return _handshake_reply({"api_key": "peer-key-unused", "name": "Laptop",
                                 "transferdaemon": {"node_id": "td-laptop", "invite": "inv-td-laptop"}}, url)

    monkeypatch.setattr(peers, "_post_with_retry", fake_post)
    token = peers._encode_pairing_token("http://10.0.2.15:8787", "secret",
                                        {"invite": "inv-td-laptop", "node_id": "td-laptop"})
    monkeypatch.setattr(db, "create_server_pairing_token", lambda *a, **k: ("secret", "2099-01-01T00:00:00+00:00"))
    peer = asyncio.run(peers.link_peer("Laptop", token, "Home", setup_ssh=False))

    assert seen["url"] == "http://127.0.0.1:19000/td-laptop/api/peers/handshake", seen["url"]
    assert seen["payload"]["transferdaemon"]["node_id"] == "td-self"        # our own identity rides along
    assert td.paired == {"inv-td-laptop": "td-laptop"}                      # the two daemons are paired
    assert peer["td_node_id"] == "td-laptop"
    assert peer["route"]["kind"] == "direct" and peer["route"]["local_addr"] == "http://127.0.0.1:19000/td-laptop"
    assert peers.peer_base_url(peer) == "http://127.0.0.1:19000/td-laptop"


def test_the_receiving_side_pairs_and_publishes_to_paired_nodes_only(temp_db, td):
    secret, _expires_at = db.create_server_pairing_token()
    td.exposed.clear()
    out = asyncio.run(peers.accept_handshake(
        "Home", "key-unused", "http://10.0.2.15:8787", "Laptop", secret,
        transferdaemon={"node_id": "td-home", "invite": "inv-td-home"}))
    assert td.paired == {"inv-td-home": "td-home"}
    assert td.exposed[SERVICE]["allow"] == ["*paired"], td.exposed
    assert out["transferdaemon"]["node_id"] == "td-self", out
    row = db.list_peer_servers()[0]
    assert row["td_node_id"] == "td-home"


# ---- a peer reached only through overlay.connect ---------------------------------------------------------------------
def test_a_peer_with_an_unreachable_address_is_reached_over_the_overlay(peer, td):
    assert peer["base_url"] == "http://10.0.2.15:8787"
    assert peers.refresh_route(peer) is True
    assert peers.peer_base_url(peer) == "http://127.0.0.1:19000/td-laptop"
    info = peers.route_info(peer)
    assert (info["kind"], info["state"], info["td_node_id"]) == ("direct", "up", "td-laptop")


def test_without_a_route_the_direct_address_is_used(peer, td):
    """A peer linked before TransferDaemon was in the picture keeps working exactly as before."""
    db.set_peer_transport(peer["id"], td_node_id="")
    row = db.get_peer_server(peer["id"])
    assert peers.refresh_route(row) is False
    assert peers.peer_base_url(row) == "http://10.0.2.15:8787"
    assert peers.route_info(row)["kind"] == "direct"
    assert td.calls == [] or all(op != "overlay.connect" for op, _ in td.calls)


# ---- a route change mid-heartbeat -------------------------------------------------------------------------------------
def test_a_route_change_picks_itself_up_on_the_heartbeat(peer, td, monkeypatch):
    peers.refresh_route(peer)
    first = peers.peer_base_url(peer)

    td.connects["td-laptop"] = "http://127.0.0.1:19111/td-laptop"
    td.route = {"kind": "relay", "rtt_ms": 140.0, "loss": 0.02, "mbps": 4.0, "state": "up"}
    assert asyncio.run(peers.health_check_all()) is None
    after = peers.route_info(peer)
    assert peers.peer_base_url(peer) == "http://127.0.0.1:19111/td-laptop"
    assert (after["kind"], after["rtt_ms"], after["loss"]) == ("relay", 140.0, 0.02)
    assert first == "http://127.0.0.1:19000/td-laptop"

    td.connects["td-laptop"] = "http://127.0.0.1:19222/td-laptop"
    td.route = {"kind": "direct", "rtt_ms": 8.0, "loss": 0.0, "mbps": 950.0, "state": "up"}
    asyncio.run(peers.health_check_all())
    assert peers.peer_base_url(peer) == "http://127.0.0.1:19222/td-laptop"
    assert peers.route_info(peer)["kind"] == "direct"


def test_the_routes_endpoint_shows_kind_rtt_and_state(peer, td):
    peers.refresh_route(peer)
    routes = peers.td_routes()
    assert routes["Laptop"]["kind"] == "direct" and routes["Laptop"]["rtt_ms"] == 12.0
    assert routes["Laptop"]["state"] == "up" and routes["Laptop"]["local_addr"].endswith("/td-laptop")


# ---- placement on route metrics ---------------------------------------------------------------------------------------
def _node(name: str, *, peer=None, rtt=None, latency=None, free_cpu=2.0):
    return {"name": name, "peer": peer, "health": "ok", "latency_ms": latency, "info": {
        "available": True, "offer": {"kinds": ["python"]}, "static": {"os": "windows", "hypervisors": [],
                                                                     "toolchains": []},
        "free": {"cpu": free_cpu, "ram_gb": 4.0, "disk_gb": 10.0, "slots": 2, "gpus": [], "vram_gb": {}},
        "capacity": {"cpu": 4.0, "ram_gb": 8.0}, "live": {"cpu_pct": 10.0}, "software": {"modules": []}},
        "route": ({"kind": "overlay", "rtt_ms": rtt, "loss": 0.0, "state": "up", "td_node_id": peer}
                  if rtt is not None else None)}


def test_placement_prefers_the_closer_node_by_route_metrics(monkeypatch):
    from bot.cluster.offer import Request
    nodes = [_node("Near", peer="td-near", rtt=5.0, latency=900.0), _node("Far", peer="td-far", rtt=180.0, latency=4.0)]
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: nodes)
    fits, _ = scheduler.candidates(Request(cpu=1), "python")
    assert [n["name"] for n in fits] == ["Near", "Far"]


def test_a_lossy_route_counts_for_more_than_its_rtt(monkeypatch):
    from bot.cluster.offer import Request
    clean = _node("Clean", peer="td-a", rtt=40.0)
    lossy = _node("Lossy", peer="td-b", rtt=20.0)
    lossy["route"]["loss"] = 0.5
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: [clean, lossy])
    fits, _ = scheduler.candidates(Request(cpu=1), "python")
    assert [n["name"] for n in fits] == ["Clean", "Lossy"], "20 ms at 50% loss is not closer than 40 ms clean"


def test_without_routes_placement_uses_the_measured_latency(monkeypatch):
    from bot.cluster.offer import Request
    nodes = [_node("Close", peer="td-a", latency=3.0), _node("Far", peer="td-b", latency=300.0)]
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: nodes)
    fits, _ = scheduler.candidates(Request(cpu=1), "python")
    assert [n["name"] for n in fits] == ["Close", "Far"]


def test_a_gang_can_target_a_transferdaemon_service_group(monkeypatch, td):
    from bot.cluster.offer import Request
    td.group = {"gpu-boxes": ["td-a", "td-b"]}
    nodes = [_node("A", peer="td-a", rtt=5.0), _node("B", peer="td-b", rtt=9.0), _node("C", peer="td-c", rtt=1.0)]
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: nodes)
    fits, why_not = scheduler.candidates(Request(cpu=1), "python", service_group="gpu-boxes")
    assert [n["name"] for n in fits] == ["A", "B"]
    assert "gpu-boxes" in why_not["C"], why_not


# ---- a big result moved by TD's file transfer -------------------------------------------------------------------------
def test_a_big_result_is_moved_by_transferdaemons_own_transfer(peer, td, tmp_path, monkeypatch):
    store.use_path(tmp_path / "cluster.sqlite3")
    monkeypatch.setattr(scheduler, "work_root", lambda: tmp_path / "work")
    big = tmp_path / "work" / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    small = tmp_path / "work" / "small.txt"
    small.write_bytes(b"tiny")

    # The node's side of the real code: it asks its own TransferDaemon to send the file, which really moves the
    # bytes into the downloads folder. Only the hop across the peer link (which a stand-in stands in for) is
    # replaced.
    async def fake_node_call(node_name, method, path, body=None, timeout=60):
        assert method == "POST" and path.endswith("/send"), path
        return await asyncio.to_thread(overlay.send_file, big, str(body["to"]))

    monkeypatch.setattr(scheduler, "_node_call", fake_node_call)
    monkeypatch.setattr(overlay, "node_id", lambda: "td-home")
    store.put("placements", {"id": "p1", "run_id": "r1", "peer": "Laptop", "state": "done",
                             "run": {"files": [{"name": "big.bin", "size": big.stat().st_size},
                                               {"name": "small.txt", "size": small.stat().st_size}]}})
    out = asyncio.run(scheduler.collect("p1", min_bytes=1024 * 1024, wait_s=2))
    assert [f["name"] for f in out["files"]] == ["big.bin"]
    assert Path(out["files"][0]["path"]).name == "big.bin"
    assert td.sent[-1]["contact_id"] == "td-home"
    assert td.sent[-1]["size"] == 2 * 1024 * 1024
    assert td.sent[-1]["file_path"] == str(big)          # the node pushes, not the asker pulling
    assert store.get("placements", "p1")["fetched"][0]["name"] == "big.bin"
    store.use_path(None)


def test_small_results_stay_on_the_plain_route(peer, td, tmp_path, monkeypatch):
    store.use_path(tmp_path / "cluster.sqlite3")
    store.put("placements", {"id": "p2", "run_id": "r2", "peer": "Laptop", "state": "done",
                             "run": {"files": [{"name": "small.txt", "size": 12}]}})
    called = []
    monkeypatch.setattr(scheduler, "_node_call", lambda *a, **k: called.append(a))
    out = asyncio.run(scheduler.collect("p2", min_bytes=1024 * 1024, wait_s=1))
    assert out["files"] == [] and called == [] and "note" in out
    assert td.sent == []
    store.use_path(None)


def test_the_node_route_sends_its_own_result_file(peer, td, tmp_path, monkeypatch):
    from bot.cluster import executor
    monkeypatch.setattr(executor, "_job_dir", lambda jid: tmp_path / jid)
    (tmp_path / "r3" / "out").mkdir(parents=True)
    (tmp_path / "r3" / "out" / "weights.bin").write_bytes(b"y" * 2048)
    store.use_path(tmp_path / "cluster.sqlite3")
    store.put("runs", {"id": "r3", "kind": "python", "state": "done", "peer": "Laptop"})

    out = scheduler._local("POST", "/runs/r3/files/weights.bin/send", {"to": "td-home"})
    assert out["file_name"] == "weights.bin"
    assert td.sent[-1] == {"contact_id": "td-home", "file_path": str(tmp_path / "r3" / "out" / "weights.bin"),
                           "size": 2048}
    with pytest.raises(Exception):
        scheduler._local("POST", "/runs/r3/files/../job.log/send", {"to": "td-home"})
    store.use_path(None)


# ---- no firewall in the default path ----------------------------------------------------------------------------------
def test_pairing_and_the_heartbeat_never_call_the_firewall(temp_db, td, monkeypatch):
    from bot import firewall
    monkeypatch.setattr(firewall, "has_inbound_rule", lambda port: pytest.fail("pairing checked the firewall"))
    monkeypatch.setattr(firewall, "open_inbound_port",
                        lambda port: pytest.fail("pairing tried to open a port"))
    peers.generate_pairing_token("http://192.168.1.20:8787", "home")

    async def fake_post(client, url, **kw):
        return _handshake_reply({"api_key": "peer-key-unused", "name": "Laptop"}, url)

    monkeypatch.setattr(peers, "_post_with_retry", fake_post)
    token = peers._encode_pairing_token("http://192.168.1.20:8787", "s", {"invite": "inv-td-laptop",
                                                                         "node_id": "td-laptop"})
    asyncio.run(peers.link_peer("Laptop", token, "Home", setup_ssh=False))
    asyncio.run(peers.health_check_all())


# ---- the fallback without TransferDaemon -------------------------------------------------------------------------------
def test_without_td_pairing_is_exactly_the_old_direct_behaviour(temp_db, monkeypatch):
    monkeypatch.setattr(client, "find", lambda: None)
    monkeypatch.setattr(overlay, "available", lambda: False)
    monkeypatch.setattr(db, "create_server_pairing_token", lambda *a, **k: ("secret", "2099-01-01T00:00:00+00:00"))
    monkeypatch.setattr(harness, "install_dir", lambda: Path("/nonexistent/td"))
    monkeypatch.setattr(harness, "_is_checkout", lambda p: False)
    monkeypatch.setattr(harness, "binary", lambda name: Path("/nonexistent/td/transferd"))

    token = peers.generate_pairing_token("http://192.168.1.20:8787", "home")
    base_url, secret, carried = peers._decode_pairing_token(token["pairing_token"])
    assert carried == {} and base_url == "http://192.168.1.20:8787"
    assert token["transferdaemon"]["available"] is False
    assert token["transferdaemon"]["offer_install"] is True
    assert "TransferDaemon is not installed" in token["transferdaemon"]["note"]

    calls: list[str] = []

    async def fake_post(client_, url, **kw):
        calls.append(url)
        return _handshake_reply({"api_key": "peer-key-unused", "name": "Laptop"}, url)

    monkeypatch.setattr(peers, "_post_with_retry", fake_post)
    peer = asyncio.run(peers.link_peer("Laptop", token["pairing_token"], "Home", setup_ssh=False))
    assert calls == ["http://192.168.1.20:8787/api/peers/handshake"], calls
    assert peer["base_url"] == "http://192.168.1.20:8787"
    assert peer["td_node_id"] == ""
    assert peers.peer_base_url(peer) == "http://192.168.1.20:8787"
    assert peers.route_info(peer)["kind"] == "direct"


def test_a_link_failure_never_blames_the_firewall(temp_db, monkeypatch):
    monkeypatch.setattr(db, "create_server_pairing_token", lambda *a, **k: ("secret", "2099-01-01T00:00:00+00:00"))
    monkeypatch.setattr(overlay, "available", lambda: False)

    async def boom(client_, url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(peers, "_post_with_retry", boom)
    token = peers.generate_pairing_token("http://192.168.1.20:8787", "home")["pairing_token"]
    with pytest.raises(peers.PeerError) as e:
        asyncio.run(peers.link_peer("Laptop", token, "Home", setup_ssh=False))
    msg = str(e.value)
    assert "TransferDaemon" in msg and "across networks" in msg
    assert "no firewall rule and no forwarded port" in msg
    assert "firewall is the most common culprit" not in msg


def test_starting_abp_starts_an_installed_transferdaemon(monkeypatch, tmp_path):
    started = {}
    exe = tmp_path / "already-built" / "transferd"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(client, "find", lambda: None)
    monkeypatch.setattr(harness, "binary", lambda name: exe)
    monkeypatch.setattr(harness, "start_daemon",
                        lambda wait_s=90.0: started.update(wait_s=wait_s) or {"running": True})
    assert peers.ensure_transport() == {"running": True, "started": True, "wait_s": 30.0}
    assert started == {"wait_s": 30.0}


def test_starting_abp_never_installs_transferdaemon(monkeypatch):
    monkeypatch.setattr(client, "find", lambda: None)
    monkeypatch.setattr(harness, "binary", lambda name: Path("/nonexistent/transferd"))
    monkeypatch.setattr(harness, "setup", lambda: pytest.fail("ABP installed TransferDaemon on start-up"))
    out = peers.ensure_transport()
    assert out == {"running": False, "installed": False, "offer_install": True}


# ---- the API ----------------------------------------------------------------------------------------------------------
def test_the_transport_and_routes_endpoints(temp_db, td, peer, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app
    c = TestClient(build_app())
    headers = {"X-Dashboard-Token": "test-token"}
    assert c.get("/api/peers/routes", headers=headers).json()["Laptop"]["td_node_id"] == "td-laptop"
    assert c.get("/api/peers/transport", headers=headers).json()["available"] is True
    rows = c.get("/api/peers", headers=headers).json()
    assert rows[0]["route"]["td_node_id"] == "td-laptop"
    assert rows[0]["reachable_at"] == "http://10.0.2.15:8787"
    assert "outbound_api_key" not in rows[0]


# ---- against a real TransferDaemon, when one is built ----------------------------------------------------------------
def _live_build() -> Path | None:
    from bot.transferdaemon import harness
    if not harness.binary("transferd").is_file():
        return None
    return harness.bin_dir()


@pytest.mark.skipif(_live_build() is None, reason="no TransferDaemon build next to ABP")
def test_the_overlay_against_a_real_daemon(tmp_path, monkeypatch):
    import socket

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    from bot.transferdaemon import harness
    monkeypatch.setenv("TRANSFERD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRANSFERD_ADDR", f"127.0.0.1:{free_port()}")
    monkeypatch.setenv("TRANSFERD_CONTROL_ADDR", f"127.0.0.1:{free_port()}")
    monkeypatch.setenv("TRANSFERD_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(harness, "bin_dir", _live_build)
    monkeypatch.setattr(harness, "install_dir", lambda: _live_build().parents[2])
    monkeypatch.setattr(harness, "_is_checkout", lambda p: True)
    client._ops.update(at=0.0, url="", ops=[])
    overlay.forget_cache()
    peers._routes.clear()
    try:
        harness.start_daemon(wait_s=120)
        ids = {o["id"] for o in client.operations()}
        missing = sorted(set(OPS) - ids)
        if missing:
            pytest.skip(f"this TransferDaemon build has no overlay yet (missing: {', '.join(missing)})")
        assert overlay.available() is True
        st = overlay.status()
        assert st["node_id"]
        assert overlay.invite_create(60)["invite"]
        exposed = overlay.expose("http://127.0.0.1:1")
        assert exposed["allow"] == ["*paired"]
        assert overlay.proxy_status() is not None
        assert overlay.connect(node_id="no-such-node", service=SERVICE) is None
        big = tmp_path / "big.bin"
        big.write_bytes(b"z" * (2 * 1024 * 1024))
        sent = overlay.send_file(big, st["node_id"])
        assert sent.get("id") or sent.get("transfer_id")
        assert overlay.unexpose() is True
    finally:
        harness.stop_daemon()
        overlay.forget_cache()
        peers._routes.clear()
