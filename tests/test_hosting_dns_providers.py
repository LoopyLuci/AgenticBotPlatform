"""Every DNS provider ABP manages records with (bot/hosting/dns.py) against a small stateful stand-in of that provider's
API (httpx.MockTransport): its own paths, field names, priorities, TXT quoting, record sets, and errors. Provider APIs
need a person's account, so these stand-ins hold the zone the way the real API does and the same scenario runs on
each: sign-in check, zones, a two-address record set, an in-place change, MX and TXT, a CNAME replacing the
addresses, deletion."""
from __future__ import annotations

import itertools
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from bot.hosting.store import HostingError

ZONE = "example.com"


@pytest.fixture
def hosting(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_HOSTING_DIR", str(tmp_path / "hosting"))
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ABP_VAULT_KEY", raising=False)
    return tmp_path / "hosting"


def _json(req: httpx.Request) -> dict:
    return json.loads(req.content) if req.content else {}


def _ok(data=None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data) if data is not None else httpx.Response(status)


class Records:
    """A zone's single records: [{"id", "name" (relative, "" = apex), "type", "value", "ttl", "prio"}]."""

    def __init__(self):
        self.rows: list[dict] = []
        self.ids = itertools.count(100)
        self.calls: list[str] = []

    def add(self, name, rtype, value, ttl, prio=None):
        self.rows.append({"id": next(self.ids), "name": name, "type": rtype, "value": value, "ttl": ttl, "prio": prio})

    def get(self, rid):
        return next(r for r in self.rows if str(r["id"]) == str(rid))

    def drop(self, rid):
        self.rows = [r for r in self.rows if str(r["id"]) != str(rid)]


def vultr(z: Records):
    def h(req: httpx.Request) -> httpx.Response:
        p = req.url.path.removeprefix("/v2")
        z.calls.append(f"{req.method} {p}")
        if req.headers["authorization"] != "Bearer unused":
            return _ok({"error": "Invalid API token."}, 401)
        if p == "/account":
            return _ok({"account": {"email": "me@example.com"}})
        if p == "/domains":
            return _ok({"domains": [{"domain": ZONE}]})
        m = re.fullmatch(rf"/domains/{ZONE}/records(?:/(\d+))?", p)
        if not m:
            return _ok({"error": "not found"}, 404)
        b = _json(req)
        if req.method == "GET":
            return _ok({"records": [{"id": r["id"], "name": r["name"], "type": r["type"], "data": r["value"], "ttl": r["ttl"],
                                     "priority": r["prio"] or -1} for r in z.rows]})
        if req.method == "POST":
            z.add(b["name"], b["type"], b["data"], b["ttl"], b.get("priority"))
            return _ok({"record": {}}, 201)
        if req.method == "PATCH":
            z.get(m[1]).update(name=b["name"], value=b["data"], ttl=b["ttl"], prio=b.get("priority"))
            return _ok(status=204)
        z.drop(m[1])
        return _ok(status=204)
    return h


def linode(z: Records):
    def h(req):
        p = req.url.path.removeprefix("/v4")
        z.calls.append(f"{req.method} {p}")
        if p == "/profile":
            return _ok({"username": "me"})
        if p == "/domains":
            return _ok({"data": [{"id": 7, "domain": ZONE}]})
        m = re.fullmatch(r"/domains/7/records(?:/(\d+))?", p)
        b = _json(req)
        if req.method == "GET":
            return _ok({"data": [{"id": r["id"], "name": r["name"], "type": r["type"], "target": r["value"], "ttl_sec": r["ttl"],
                                  "priority": r["prio"] or 0} for r in z.rows]})
        if req.method == "POST":
            assert b["ttl_sec"] >= 300                                  # Linode's minimum
            z.add(b["name"], b["type"], b["target"], b["ttl_sec"], b.get("priority"))
            return _ok({})
        if req.method == "PUT":
            z.get(m[1]).update(value=b["target"], ttl=b["ttl_sec"], prio=b.get("priority"))
            return _ok({})
        z.drop(m[1])
        return _ok({})
    return h


def porkbun(z: Records):
    def h(req):
        b = _json(req)
        p = req.url.path.removeprefix("/api/json/v3")
        z.calls.append(f"{req.method} {p}")
        if (b.get("apikey"), b.get("secretapikey")) != ("unused", "unused-secret"):
            return _ok({"status": "ERROR", "message": "Invalid API key."})
        if p == "/ping":
            return _ok({"status": "SUCCESS", "yourIp": "203.0.113.9"})
        if p == "/domain/listAll":
            return _ok({"status": "SUCCESS", "domains": [{"domain": ZONE}]})
        if p == f"/dns/retrieve/{ZONE}":
            return _ok({"status": "SUCCESS", "records": [
                {"id": str(r["id"]), "name": f"{r['name']}.{ZONE}" if r["name"] else ZONE, "type": r["type"], "content": r["value"],
                 "ttl": str(r["ttl"]), "prio": r["prio"]} for r in z.rows]})
        if p == f"/dns/create/{ZONE}":
            z.add(b["name"], b["type"], b["content"], int(b["ttl"]), b.get("prio"))
            return _ok({"status": "SUCCESS", "id": 1})
        m = re.fullmatch(rf"/dns/(edit|delete)/{ZONE}/(\d+)", p)
        if m and m[1] == "edit":
            z.get(m[2]).update(value=b["content"], ttl=int(b["ttl"]), prio=b.get("prio"))
            return _ok({"status": "SUCCESS"})
        if m:
            z.drop(m[2])
            return _ok({"status": "SUCCESS"})
        return _ok({"status": "ERROR", "message": "unknown"}, 400)
    return h


def netlify(z: Records):
    def h(req):
        p = req.url.path.removeprefix("/api/v1")
        z.calls.append(f"{req.method} {p}")
        if p == "/user":
            return _ok({"email": "me@example.com"})
        if p == "/dns_zones":
            return _ok([{"id": "z9", "name": ZONE}])
        m = re.fullmatch(r"/dns_zones/z9/dns_records(?:/(\w+))?", p)
        b = _json(req)
        if req.method == "GET":
            return _ok([{"id": f"r{r['id']}", "hostname": r["name"], "type": r["type"], "value": r["value"], "ttl": r["ttl"],
                         "priority": r["prio"]} for r in z.rows])
        if req.method == "POST":
            z.add(b["hostname"], b["type"], b["value"], b["ttl"], b.get("priority"))
            return _ok({}, 201)
        z.drop(m[1].removeprefix("r"))
        return _ok(status=204)
    return h


def vercel(z: Records):
    def h(req):
        p = req.url.path
        z.calls.append(f"{req.method} {p}?{req.url.query.decode()}")
        assert parse_qs(req.url.query.decode()).get("teamId") == ["team_1"]
        if p == "/v2/user":
            return _ok({"user": {"username": "me"}})
        if p == "/v5/domains":
            return _ok({"domains": [{"name": ZONE}]})
        if p == f"/v4/domains/{ZONE}/records":
            return _ok({"records": [{"id": f"rec_{r['id']}", "name": r["name"], "type": r["type"], "value": r["value"], "ttl": r["ttl"],
                                     "mxPriority": r["prio"]} for r in z.rows]})
        b = _json(req)
        if req.method == "POST" and p == f"/v2/domains/{ZONE}/records":
            z.add(b["name"], b["type"], b["value"], b["ttl"], b.get("mxPriority"))
            return _ok({"uid": "x"})
        m = re.fullmatch(r"/v1/domains/records/rec_(\d+)", p)
        if req.method == "PATCH" and m:
            assert "type" not in b                                     # Vercel's PATCH refuses a type
            z.get(m[1]).update(value=b["value"], ttl=b["ttl"], prio=b.get("mxPriority"))
            return _ok({})
        m = re.fullmatch(rf"/v2/domains/{ZONE}/records/rec_(\d+)", p)
        z.drop(m[1])
        return _ok({})
    return h


SINGLE = [("vultr", {"api_key": "unused"}, vultr, "me@example.com"),
          ("linode", {"token": "unused"}, linode, "user me"),
          ("porkbun", {"api_key": "unused", "secret_key": "unused-secret"}, porkbun, "203.0.113.9"),
          ("netlify", {"token": "unused"}, netlify, "me@example.com"),
          ("vercel", {"token": "unused", "team_id": "team_1"}, vercel, "user me")]


@pytest.mark.parametrize("kind,fields,api,who", SINGLE, ids=[s[0] for s in SINGLE])
def test_single_record_providers(hosting, kind, fields, api, who):
    from bot.hosting import accounts, dns
    z = Records()
    acc = accounts.add(kind, kind, fields)
    p = dns.provider(acc["id"], transport=httpx.MockTransport(api(z)))
    assert who in p.verify() and ZONE in p.verify()
    assert p.zone_for(f"a.b.{ZONE}") == ZONE
    p.set(ZONE, "www", "A", ["192.0.2.1", "192.0.2.2"], ttl=600)
    sets = {(s["name"], s["type"]): s for s in p.records(ZONE)}
    assert sorted(sets[(f"www.{ZONE}", "A")]["values"]) == ["192.0.2.1", "192.0.2.2"]
    ids = {r["value"]: r["id"] for r in z.rows}
    p.set(ZONE, "www", "A", ["192.0.2.1", "192.0.2.3"], ttl=600)     # one kept, one changed
    now = {r["value"]: r["id"] for r in z.rows}
    assert now["192.0.2.1"] == ids["192.0.2.1"] and "192.0.2.2" not in now
    if kind != "netlify":                                            # an update in place (Netlify has none: remove, create)
        assert now["192.0.2.3"] == ids["192.0.2.2"]
    assert sorted(next(s for s in p.records(ZONE) if s["type"] == "A")["values"]) == ["192.0.2.1", "192.0.2.3"]
    p.set(ZONE, "@", "MX", ["10 mail.example.com"], ttl=600)
    p.set(ZONE, "@", "TXT", ["v=spf1 -all"], ttl=600)
    sets = {(s["name"], s["type"]): s["values"] for s in p.records(ZONE)}
    assert sets[(ZONE, "MX")] == ["10 mail.example.com"] and sets[(ZONE, "TXT")] == ["v=spf1 -all"]
    p.set(ZONE, "www", "CNAME", ["target.example.net"], ttl=600)     # replaces the address records
    www = [s for s in p.records(ZONE) if s["name"] == f"www.{ZONE}"]
    assert [(s["type"], s["values"]) for s in www] == [("CNAME", ["target.example.net"])]
    assert p.delete(ZONE, "www", "CNAME") == 1 and p.delete(ZONE, "www", "CNAME") == 0
    assert {s["type"] for s in p.records(ZONE)} == {"MX", "TXT"}
    with pytest.raises(HostingError, match="not in any zone"):
        p.zone_for("example.org")
    with pytest.raises(HostingError, match="IPv4"):
        p.set(ZONE, "x", "A", ["not-an-ip"])


def test_a_provider_error_says_what_the_provider_said(hosting):
    from bot.hosting import accounts, dns
    p = dns.provider(accounts.add("vultr", "bad", {"api_key": "wrong"})["id"], transport=httpx.MockTransport(vultr(Records())))
    with pytest.raises(HostingError, match=r"Vultr: GET /account failed \(401\): Invalid API token"):
        p.verify()
    q = dns.provider(accounts.add("porkbun", "bad-pb", {"api_key": "x", "secret_key": "y"})["id"],
                     transport=httpx.MockTransport(porkbun(Records())))
    with pytest.raises(HostingError, match="Invalid API key"):
        q.verify()

    def down(req):
        raise httpx.ConnectError("no route", request=req)
    r = dns.provider(accounts.add("linode", "down", {"token": "unused"})["id"], transport=httpx.MockTransport(down))
    with pytest.raises(HostingError, match="cannot reach"):
        r.zones()
    with pytest.raises(HostingError, match="does not manage DNS"):
        dns.provider(accounts.add("ftp", "f", {"host": "h", "user": "u", "password": "p"}))


# ---- record-set providers ------------------------------------------------------------------------------------------ #

class Sets:
    """A zone's record sets: {(relative name or "@", type): {"ttl", "values"}} as the API stores them (TXT quoted)."""

    def __init__(self):
        self.sets: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []


def gandi(z: Sets):
    def h(req):
        p = unquote(req.url.path.removeprefix("/v5/livedns"))
        z.calls.append(f"{req.method} {p}")
        if p == "/domains":
            return _ok([{"fqdn": ZONE}])
        if p == f"/domains/{ZONE}/records" and req.method == "GET":
            return _ok([{"rrset_name": n, "rrset_type": t, "rrset_ttl": s["ttl"], "rrset_values": s["values"]} for (n, t), s in z.sets.items()])
        m = re.fullmatch(rf"/domains/{ZONE}/records/([^/]+)/(\w+)", p)
        if req.method == "PUT":
            b = _json(req)
            z.sets[(m[1], m[2])] = {"ttl": b["rrset_ttl"], "values": b["rrset_values"]}
            return _ok({"message": "DNS Record Created"}, 201)
        if (m[1], m[2]) not in z.sets:
            return _ok({"message": "not found"}, 404)
        del z.sets[(m[1], m[2])]
        return _ok(status=204)
    return h


def hetzner(z: Sets):
    def h(req):
        p = unquote(req.url.path.removeprefix("/v1"))
        z.calls.append(f"{req.method} {p}")
        if p == "/zones":
            return _ok({"zones": [{"id": 3, "name": ZONE}]})
        if p == f"/zones/{ZONE}/rrsets" and req.method == "GET":
            page = int(parse_qs(req.url.query.decode())["page"][0])
            items = [{"name": n, "type": t, "ttl": s["ttl"], "records": [{"value": v} for v in s["values"]]} for (n, t), s in z.sets.items()]
            return _ok({"rrsets": items[(page - 1) * 1: page * 1], "meta": {"pagination": {"next_page": page + 1 if page < len(items) else None}}})
        b = _json(req)
        if p == f"/zones/{ZONE}/rrsets":
            if (b["name"], b["type"]) in z.sets:
                return _ok({"error": {"message": "rrset exists"}}, 409)
            z.sets[(b["name"], b["type"])] = {"ttl": b["ttl"], "values": [r["value"] for r in b["records"]]}
            return _ok({}, 201)
        m = re.fullmatch(rf"/zones/{ZONE}/rrsets/([^/]+)/(\w+)(/actions/(\w+))?", p)
        key = (m[1], m[2])
        if m[4] == "set_records":
            z.sets[key]["values"] = [r["value"] for r in b["records"]]
        elif m[4] == "change_ttl":
            z.sets[key]["ttl"] = b["ttl"]
        else:
            z.sets.pop(key, None)
        return _ok({})
    return h


def route53(z: Sets):
    ns = "https://route53.amazonaws.com/doc/2013-04-01/"

    def h(req):
        p = req.url.path
        z.calls.append(f"{req.method} {p}")
        assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDUNUSED/") and "x-amz-date" in req.headers
        if p == "/2013-04-01/hostedzone":
            return httpx.Response(200, content=f'<ListHostedZonesResponse xmlns="{ns}"><HostedZones><HostedZone><Id>/hostedzone/Z1</Id>'
                                                f"<Name>{ZONE}.</Name></HostedZone></HostedZones></ListHostedZonesResponse>".encode())
        if req.method == "GET":
            sets = "".join(f"<ResourceRecordSet><Name>{(n + '.' if n != '@' else '') + ZONE}.</Name><Type>{t}</Type><TTL>{s['ttl']}</TTL>"
                           f"<ResourceRecords>{''.join(f'<ResourceRecord><Value>{v}</Value></ResourceRecord>' for v in s['values'])}"
                           f"</ResourceRecords></ResourceRecordSet>" for (n, t), s in z.sets.items())
            return httpx.Response(200, content=f'<ListResourceRecordSetsResponse xmlns="{ns}"><ResourceRecordSets>{sets}'
                                                f"</ResourceRecordSets></ListResourceRecordSetsResponse>".encode())
        x = ET.fromstring(req.content)
        q = lambda e, t: e.findtext(f"{{{ns}}}{t}")  # noqa: E731
        for ch in x.iter(f"{{{ns}}}Change"):
            rr = ch.find(f"{{{ns}}}ResourceRecordSet")
            name = q(rr, "Name").rstrip(".")
            rel = "@" if name == ZONE else name[: -(len(ZONE) + 1)]
            key = (rel, q(rr, "Type"))
            if q(ch, "Action") == "DELETE":
                if key not in z.sets:
                    return httpx.Response(400, content=b"<ErrorResponse><Error><Message>it was not found</Message></Error></ErrorResponse>")
                del z.sets[key]
            else:
                z.sets[key] = {"ttl": int(q(rr, "TTL")), "values": [v.text for v in rr.iter(f"{{{ns}}}Value")]}
        return httpx.Response(200, content=f'<ChangeResourceRecordSetsResponse xmlns="{ns}"/>'.encode())
    return h


SETS = [("gandi", {"token": "unused"}, gandi), ("hetzner", {"token": "unused"}, hetzner),
        ("route53", {"access_key_id": "AKIDUNUSED", "secret_access_key": "unused"}, route53)]


@pytest.mark.parametrize("kind,fields,api", SETS, ids=[s[0] for s in SETS])
def test_record_set_providers(hosting, kind, fields, api):
    from bot.hosting import accounts, dns
    z = Sets()
    p = dns.provider(accounts.add(kind, kind, fields)["id"], transport=httpx.MockTransport(api(z)))
    assert ZONE in p.verify() and p.zone_for(f"www.{ZONE}") == ZONE
    p.set(ZONE, "www", "A", ["192.0.2.1", "192.0.2.2"], ttl=600)
    p.set(ZONE, "@", "TXT", ['say "hi"'])
    p.set(ZONE, "blog", "CNAME", ["host.example.net"])
    sets = {(s["name"], s["type"]): s["values"] for s in p.records(ZONE)}
    assert sorted(sets[(f"www.{ZONE}", "A")]) == ["192.0.2.1", "192.0.2.2"]
    assert sets[(ZONE, "TXT")] == ['say "hi"'] and sets[(f"blog.{ZONE}", "CNAME")] == ["host.example.net"]
    assert any(v.startswith('"') for (n, t), s in z.sets.items() if t == "TXT" for v in s["values"])     # quoted on the wire
    p.set(ZONE, "www", "A", ["192.0.2.9"], ttl=900)                # a whole set replaced in one call
    assert [s["values"] for s in p.records(ZONE) if s["type"] == "A"] == [["192.0.2.9"]]
    assert p.delete(ZONE, "blog", "CNAME") == 1
    assert (f"blog.{ZONE}", "CNAME") not in {(s["name"], s["type"]) for s in p.records(ZONE)}
    if kind == "route53":
        assert p.delete(ZONE, "nothing", "A") == 0
    flat = sorted((r["name"], r["type"], r["value"]) for r in p._list(ZONE))       # RRsetProvider flattens for set()'s users
    assert (f"www.{ZONE}", "A", "192.0.2.9") in flat


def test_duck_dns_updates_through_its_url(hosting):
    from bot.hosting import accounts, dns
    seen = []

    def duck(req):
        q = parse_qs(urlparse(str(req.url)).query, keep_blank_values=True)
        seen.append({k: v[0] for k, v in q.items()})
        return httpx.Response(200, text="OK" if q["token"] == ["unused"] and q["domains"] == ["mysite"] else "KO",
                              headers={"content-type": "text/plain"})
    p = dns.provider(accounts.add("duckdns", "duck", {"token": "unused", "domains": "mysite.duckdns.org"})["id"],
                     transport=httpx.MockTransport(duck))
    assert "mysite.duckdns.org" in p.verify() and p.zones() == [{"id": "mysite", "name": "mysite.duckdns.org"}]
    zone = "mysite.duckdns.org"
    assert p.set(zone, "@", "A", ["192.0.2.4"])["values"] == ["192.0.2.4"] and seen[-1]["ip"] == "192.0.2.4"
    p.set(zone, "@", "AAAA", ["2001:db8::4"])
    assert seen[-1]["ipv6"] == "2001:db8::4"
    p.set(zone, "@", "TXT", ["challenge-token"])
    assert seen[-1]["txt"] == "challenge-token"
    assert p.delete(zone, "@", "TXT") == 1 and seen[-1]["clear"] == "true" and seen[-1]["txt"] == ""
    assert p.delete(zone, "@", "A") == 1
    with pytest.raises(HostingError, match="only has records on the subdomain"):
        p.set(zone, "www", "A", ["192.0.2.4"])
    with pytest.raises(HostingError, match="only A, AAAA and TXT"):
        p.set(zone, "@", "MX", ["10 mail.example.com"])
    bad = dns.provider(accounts.add("duckdns", "duck2", {"token": "wrong", "domains": "mysite"})["id"], transport=httpx.MockTransport(duck))
    with pytest.raises(HostingError, match="refused the update"):
        bad.verify()


def test_find_zone_picks_the_account_with_the_longest_zone(hosting, monkeypatch):
    from bot.hosting import accounts, dns
    z1, z2 = Records(), Sets()
    a1 = accounts.add("vultr", "v", {"api_key": "unused"})
    a2 = accounts.add("gandi", "g", {"token": "unused"})

    def gandi_sub(z):
        inner = gandi(z)

        def h(req):
            if req.url.path.endswith("/domains"):
                return _ok([{"fqdn": f"shop.{ZONE}"}])
            return inner(req)
        return h
    transports = {a1["id"]: httpx.MockTransport(vultr(z1)), a2["id"]: httpx.MockTransport(gandi_sub(z2))}
    real = dns.provider
    monkeypatch.setattr(dns, "provider", lambda acc, transport=None: real(acc, transports[acc if isinstance(acc, str) else acc["id"]]))
    p, zone = dns.find_zone(f"www.shop.{ZONE}")
    assert (p.label, zone) == ("Gandi", f"shop.{ZONE}")
    p, zone = dns.find_zone(f"www.{ZONE}")
    assert (p.label, zone) == ("Vultr", ZONE)
    with pytest.raises(HostingError, match="none of the connected DNS accounts"):
        dns.find_zone("example.org")
