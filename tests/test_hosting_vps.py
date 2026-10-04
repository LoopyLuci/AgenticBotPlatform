"""Creating servers at the VPS providers (bot/hosting/vps.py) against stateful stand-ins of their APIs (httpx.MockTransport;
a real server costs money and needs a person's account): sizes and prices, the price a person must confirm, ABP's own
SSH key registered once and reused, cloud-init, the SSH account made for each new server, listing and destroying."""
from __future__ import annotations

import base64
import itertools
import json
import re

import httpx
import pytest

from bot.hosting.store import HostingError


@pytest.fixture
def hosting(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_HOSTING_DIR", str(tmp_path / "hosting"))
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ABP_VAULT_KEY", raising=False)
    return tmp_path / "hosting"


class Cloud:
    def __init__(self):
        self.keys: list[dict] = []
        self.servers: dict[int, dict] = {}
        self.ids = itertools.count(1)
        self.calls: list[str] = []


def _r(data=None, status=200):
    return httpx.Response(status, json=data if data is not None else {})


def hetzner(c: Cloud):
    def h(req):
        p, b = req.url.path.removeprefix("/v1"), json.loads(req.content) if req.content else {}
        c.calls.append(f"{req.method} {p}")
        if p == "/server_types":
            return _r({"server_types": [{"name": "cx22", "cores": 2, "memory": 4, "disk": 40, "architecture": "x86",
                                         "prices": [{"location": "fsn1", "price_monthly": {"gross": "4.5900"}}]},
                                        {"name": "old", "deprecation": {"announced": "x"}, "cores": 1, "memory": 1, "disk": 10, "prices": []}]})
        if p == "/locations":
            return _r({"locations": [{"name": "fsn1", "city": "Falkenstein", "country": "DE"}]})
        if p == "/ssh_keys" and req.method == "GET":
            return _r({"ssh_keys": c.keys})
        if p == "/ssh_keys":
            c.keys.append({"id": next(c.ids), "public_key": b["public_key"]})
            return _r({"ssh_key": c.keys[-1]}, 201)
        if p == "/servers" and req.method == "POST":
            assert b["ssh_keys"] == [c.keys[0]["id"]] and "ssh_authorized_keys" in b["user_data"] and b["labels"] == {"abp": "1"}
            sid = next(c.ids)
            c.servers[sid] = {"id": sid, "name": b["name"], "status": "running", "public_net": {"ipv4": {"ip": f"192.0.2.{sid}"}},
                              "server_type": {"name": b["server_type"]}, "datacenter": {"location": {"name": b["location"]}}}
            return _r({"server": c.servers[sid]}, 201)
        if p == "/servers":
            return _r({"servers": list(c.servers.values())})
        m = re.fullmatch(r"/servers/(\d+)", p)
        c.servers.pop(int(m[1]))
        return _r()
    return h


def digitalocean(c: Cloud):
    def h(req):
        p, b = req.url.path.removeprefix("/v2"), json.loads(req.content) if req.content else {}
        c.calls.append(f"{req.method} {p}")
        if p == "/account":
            return _r({"account": {"email": "me@example.com", "droplet_limit": 10}})
        if p == "/sizes":
            return _r({"sizes": [{"slug": "s-1vcpu-1gb", "vcpus": 1, "memory": 1024, "disk": 25, "price_monthly": 6.0, "regions": ["fra1"],
                                  "available": True}, {"slug": "gone", "available": False}]})
        if p == "/regions":
            return _r({"regions": [{"slug": "fra1", "name": "Frankfurt 1", "available": True}]})
        if p == "/account/keys" and req.method == "GET":
            return _r({"ssh_keys": c.keys})
        if p == "/account/keys":
            c.keys.append({"id": next(c.ids), "public_key": b["public_key"]})
            return _r({"ssh_key": c.keys[-1]}, 201)
        if p == "/droplets" and req.method == "POST":
            sid = next(c.ids)
            c.servers[sid] = {"id": sid, "name": b["name"], "status": "active", "size_slug": b["size"], "region": {"slug": b["region"]},
                              "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.2"}, {"type": "public", "ip_address": f"192.0.2.{sid}"}]}}
            return _r({"droplet": {"id": sid}}, 202)
        if p == "/droplets":
            return _r({"droplets": list(c.servers.values())})
        m = re.fullmatch(r"/droplets/(\d+)", p)
        if req.method == "GET":
            return _r({"droplet": c.servers[int(m[1])]})
        c.servers.pop(int(m[1]))
        return _r(status=204)
    return h


def vultr(c: Cloud):
    def h(req):
        p, b = req.url.path.removeprefix("/v2"), json.loads(req.content) if req.content else {}
        c.calls.append(f"{req.method} {p}")
        if p == "/account":
            return _r({"account": {"email": "me@example.com", "balance": -5}})
        if p == "/plans":
            return _r({"plans": [{"id": "vc2-1c-1gb", "vcpu_count": 1, "ram": 1024, "disk": 25, "monthly_cost": 5, "locations": ["ams"]}]})
        if p == "/regions":
            return _r({"regions": [{"id": "ams", "city": "Amsterdam", "country": "NL"}]})
        if p == "/os":
            return _r({"os": [{"id": 1743, "name": "Ubuntu 22.04 LTS x64"}, {"id": 2284, "name": "Ubuntu 24.04 LTS x64"}]})
        if p == "/ssh-keys" and req.method == "GET":
            return _r({"ssh_keys": c.keys})
        if p == "/ssh-keys":
            c.keys.append({"id": f"k{next(c.ids)}", "ssh_key": b["ssh_key"]})
            return _r({"ssh_key": c.keys[-1]}, 201)
        if p == "/instances" and req.method == "POST":
            assert b["os_id"] == 2284 and "ssh_authorized_keys" in base64.b64decode(b["user_data"]).decode()
            sid = next(c.ids)
            c.servers[sid] = {"id": f"i{sid}", "label": b["label"], "status": "active", "main_ip": f"192.0.2.{sid}", "plan": b["plan"],
                              "region": b["region"]}
            return _r({"instance": c.servers[sid]}, 202)
        if p == "/instances":
            return _r({"instances": list(c.servers.values())})
        m = re.fullmatch(r"/instances/i(\d+)", p)
        c.servers.pop(int(m[1]))
        return _r(status=204)
    return h


def linode(c: Cloud):
    def h(req):
        p, b = req.url.path.removeprefix("/v4"), json.loads(req.content) if req.content else {}
        c.calls.append(f"{req.method} {p}")
        if p == "/profile":
            return _r({"username": "me"})
        if p == "/linode/types":
            return _r({"data": [{"id": "g6-nanode-1", "vcpus": 1, "memory": 1024, "disk": 25600, "price": {"monthly": 5.0}}]})
        if p == "/regions":
            return _r({"data": [{"id": "eu-central", "label": "Frankfurt"}]})
        if p == "/linode/instances" and req.method == "POST":
            assert b["authorized_keys"][0].startswith("ssh-ed25519 ") and len(b["root_pass"]) > 20
            sid = next(c.ids)
            c.servers[sid] = {"id": sid, "label": b["label"], "status": "running", "ipv4": [f"192.0.2.{sid}"], "type": b["type"],
                              "region": b["region"], "tags": b["tags"]}
            return _r(c.servers[sid])
        if p == "/linode/instances":
            return _r({"data": list(c.servers.values()) + [{"id": 99, "label": "not ours", "status": "running", "ipv4": [], "type": "x",
                                                             "region": "y", "tags": []}]})
        m = re.fullmatch(r"/linode/instances/(\d+)", p)
        c.servers.pop(int(m[1]))
        return _r()
    return h


CLOUDS = [("hetzner", {"token": "unused"}, hetzner, "fsn1", "cx22", 4.59, "EUR"),
          ("digitalocean", {"token": "unused"}, digitalocean, "fra1", "s-1vcpu-1gb", 6.0, "me@example.com"),
          ("vultr", {"api_key": "unused"}, vultr, "ams", "vc2-1c-1gb", 5.0, "me@example.com"),
          ("linode", {"token": "unused"}, linode, "eu-central", "g6-nanode-1", 5.0, "user me")]


@pytest.mark.parametrize("kind,fields,api,region,size,price,who", CLOUDS, ids=[c[0] for c in CLOUDS])
def test_servers_are_created_only_at_a_confirmed_price(hosting, kind, fields, api, region, size, price, who):
    from bot.hosting import accounts, vps
    c = Cloud()
    p = vps.provider(accounts.add(kind, kind, fields)["id"], transport=httpx.MockTransport(api(c)))
    if kind != "hetzner":
        assert who in p.verify()
    opts = p.options()
    assert [s["id"] for s in opts["sizes"]] == [size] and opts["regions"][0]["id"] == region
    with pytest.raises(HostingError, match="confirm that price"):
        p.create("web1", region, size)                                       # no price confirmed: nothing is created
    with pytest.raises(HostingError, match="confirm that price"):
        p.create("web1", region, size, confirm_monthly=price + 1)
    assert not any(call.startswith("POST") for call in c.calls)
    with pytest.raises(HostingError, match="no size"):
        p.create("web1", region, "huge", confirm_monthly=1)
    first = p.create("web1", region, size, confirm_monthly=price)
    second = p.create("web2", region, size, confirm_monthly=price)
    assert first["ip"].startswith("192.0.2.") and first["ip"] != second["ip"]
    if kind != "linode":                                                  # Linode takes the key with each server
        assert len(c.keys) == 1                                          # ABP's key registered once, then reused
    ssh = accounts.get(first["account"]["id"])
    assert ssh["provider"] == "ssh" and accounts.setting(ssh, "host") == first["ip"] and accounts.setting(ssh, "user") == "abp"
    key, pub = vps.keypair()
    assert key.exists() and pub.startswith("ssh-ed25519 ") and accounts.setting(ssh, "key_path") == str(key)
    listed = p.servers()
    assert sorted(s["name"] for s in listed) == ["web1", "web2"] and all(s["ip"] for s in listed)
    assert p.destroy(listed[0]["id"]) and len(p.servers()) == 1


def test_cloud_init_and_the_key(hosting):
    from bot.hosting import vps
    ud = vps.user_data()
    _, pub = vps.keypair()
    assert ud.startswith("#cloud-config") and pub in ud and "caddy" in ud
    assert vps.keypair()[1] == pub                                          # made once
    from bot.hosting import accounts
    with pytest.raises(HostingError):
        vps.provider(accounts.add("ftp", "f", {"host": "h", "user": "u", "password": "p"}))


def test_a_new_servers_account_always_has_a_valid_unique_name(hosting):
    from bot.hosting import accounts, vps
    p = vps.provider(accounts.add("hetzner", "h", {"token": "unused"}), transport=httpx.MockTransport(hetzner(Cloud())))
    a = p.after_create("web(1)!", "192.0.2.1")
    b = p.after_create("web(1)!", "192.0.2.2")
    assert a["name"] == "web-1 on Hetzner Cloud" and b["name"] == "web-1 on Hetzner Cloud 2"
