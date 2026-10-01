"""One interface over every DNS provider ABP can manage.

    p = dns.provider(account)          # an account from bot/hosting/accounts.py
    p.verify()                         # a read-only call; a short note on success, HostingError if refused
    p.zones()                          # [{"id", "name"}]
    p.records(zone)                    # [{"name", "type", "values", "ttl", "proxied"?}], one entry per record set
    p.set(zone, name, type, values, ttl=300, proxied=None)   # the record set becomes exactly these values
    p.delete(zone, name, type)

Record sets, not single records: "www A = this IP" means "www has exactly this A record", the same whether the
provider stores record sets (deSEC, Gandi, Route 53, Hetzner) or single records (Cloudflare, DigitalOcean...), where
set() updates, adds and removes records to match. Names are full names without the trailing dot ("www.example.com",
the apex is "example.com"); a short name ("www", "@") is taken relative to the zone. `zone` is a zone's name.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote

import httpx

from bot.hosting import accounts
from bot.hosting.store import HostingError

TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA")
_TIMEOUT = 30.0
_HOST = re.compile(r"^(\*\.)?([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")


def fqdn(name: str, zone: str) -> str:
    """'www' / '@' / 'www.example.com' / 'www.example.com.' -> a full name in `zone`."""
    name = (name or "@").strip().rstrip(".").lower()
    zone = zone.strip().rstrip(".").lower()
    if name in ("@", "", zone):
        return zone
    if name.endswith("." + zone):
        return name
    return f"{name}.{zone}"


def relative(name: str, zone: str) -> str:
    """A full name -> its part inside the zone ('' for the apex)."""
    full = fqdn(name, zone)
    zone = zone.strip().rstrip(".").lower()
    return "" if full == zone else full[: -(len(zone) + 1)]


def check_record(name: str, rtype: str, values: list[str]) -> str:
    rtype = rtype.upper()
    if rtype not in TYPES:
        raise HostingError(f"record type {rtype} is not one of {', '.join(TYPES)}")
    if not _HOST.match(name):
        raise HostingError(f"{name!r} is not a valid host name")
    if not values:
        raise HostingError("a record set needs at least one value (use delete to remove it)")
    if rtype == "CNAME" and len(values) != 1:
        raise HostingError("a CNAME has exactly one value")
    for v in values:
        if rtype == "A" and not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", v):
            raise HostingError(f"{v!r} is not an IPv4 address")
        if rtype == "AAAA" and ":" not in v:
            raise HostingError(f"{v!r} is not an IPv6 address")
    return rtype


def _txt_quote(v: str) -> str:
    return v if v.startswith('"') else '"' + v.replace('"', '\\"') + '"'


def _txt_unquote(v: str) -> str:
    return v[1:-1].replace('\\"', '"') if len(v) >= 2 and v.startswith('"') and v.endswith('"') else v


class Provider:
    """Shared plumbing: an httpx client, errors turned into HostingError with the provider's own message."""
    label = "DNS"
    base = ""

    def __init__(self, acc: dict, transport: Optional[httpx.BaseTransport] = None):
        self.acc = acc
        self._transport = transport

    def headers(self) -> dict:
        return {}

    def req(self, method: str, path: str, **kw) -> Any:
        url = path if path.startswith("http") else self.base + path
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=self._transport, headers=self.headers()) as c:
                r = c.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise HostingError(f"{self.label}: cannot reach {url.split('?')[0]}: {e}") from e
        if r.status_code >= 400:
            raise HostingError(f"{self.label}: {method} {path.split('?')[0]} failed ({r.status_code}): {self._why(r)}")
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.text

    @staticmethod
    def _why(r: httpx.Response) -> str:
        try:
            j = r.json()
        except ValueError:
            return r.text[:300]
        for key in ("errors", "error", "message", "detail", "errorMessage"):
            if key in j and j[key]:
                return str(j[key])[:300]
        return str(j)[:300]

    # single-record providers implement these four; set()/delete() are built on them
    def _list(self, zone: str) -> list[dict]:                 # [{"id", "name"(full), "type", "value", "ttl", "proxied"?}]
        raise NotImplementedError

    def _create(self, zone: str, name: str, rtype: str, value: str, ttl: int, proxied: Optional[bool]) -> None:
        raise NotImplementedError

    def _update(self, zone: str, rec: dict, value: str, ttl: int, proxied: Optional[bool]) -> None:
        self._remove(zone, rec)
        self._create(zone, rec["name"], rec["type"], value, ttl, proxied)

    def _remove(self, zone: str, rec: dict) -> None:
        raise NotImplementedError

    def records(self, zone: str) -> list[dict]:
        sets: dict[tuple, dict] = {}
        for r in self._list(zone):
            key = (r["name"], r["type"])
            s = sets.setdefault(key, {"name": r["name"], "type": r["type"], "values": [], "ttl": r.get("ttl")})
            s["values"].append(r["value"])
            if "proxied" in r:
                s["proxied"] = r["proxied"]
        return sorted(sets.values(), key=lambda s: (s["name"].count("."), s["name"], s["type"]))

    def set(self, zone: str, name: str, rtype: str, values: list[str], ttl: int = 300,
            proxied: Optional[bool] = None) -> dict:
        full = fqdn(name, zone)
        rtype = check_record(full, rtype, values)
        existing = [r for r in self._list(zone) if r["name"] == full]
        if rtype in ("A", "AAAA", "CNAME"):    # a CNAME cannot sit beside an address record of the same name
            for r in existing:
                if r["type"] in ("A", "AAAA", "CNAME") and (r["type"] == "CNAME") != (rtype == "CNAME"):
                    self._remove(zone, r)
        want = list(dict.fromkeys(values))
        matched: list[str] = []
        leftover = []
        for r in (r for r in existing if r["type"] == rtype):
            if r["value"] in want and r["value"] not in matched:
                matched.append(r["value"])
                if proxied is not None and r.get("proxied") is not None and r["proxied"] != proxied:
                    self._update(zone, r, r["value"], ttl, proxied)
            else:
                leftover.append(r)
        missing = [v for v in want if v not in matched]
        for r in leftover:              # reuse a stale record for a missing value; remove the rest
            if missing:
                self._update(zone, r, missing.pop(0), ttl, proxied)
            else:
                self._remove(zone, r)
        for v in missing:
            self._create(zone, full, rtype, v, ttl, proxied)
        return {"name": full, "type": rtype, "values": want, "ttl": ttl, **({"proxied": proxied} if proxied is not None else {})}

    def delete(self, zone: str, name: str, rtype: str) -> int:
        full = fqdn(name, zone)
        gone = 0
        for r in self._list(zone):
            if r["name"] == full and r["type"] == rtype.upper():
                self._remove(zone, r)
                gone += 1
        return gone

    def zone_for(self, host: str) -> str:
        """The zone (among this account's) that `host` belongs to: the longest zone name it ends with."""
        host = host.strip().rstrip(".").lower()
        best = ""
        for z in self.zones():
            n = z["name"].lower()
            if (host == n or host.endswith("." + n)) and len(n) > len(best):
                best = n
        if not best:
            raise HostingError(f"{host} is not in any zone of {self.acc.get('name', self.label)} "
                               f"(its zones: {', '.join(z['name'] for z in self.zones()) or 'none'})")
        return best


# ---------------------------------------------------------------------------------------------------------------- #

class Cloudflare(Provider):
    label = "Cloudflare"
    base = "https://api.cloudflare.com/client/v4"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'api_token')}"}

    def _pages(self, path: str) -> list[dict]:
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            j = self.req("GET", f"{path}{sep}per_page=100&page={page}")
            out += j.get("result") or []
            info = j.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                return out
            page += 1

    def verify(self):
        j = self.req("GET", "/user/tokens/verify")
        if (j.get("result") or {}).get("status") != "active":
            raise HostingError(f"Cloudflare: the token is {(j.get('result') or {}).get('status', 'not active')}")
        zones = self.zones()
        return f"token active; {len(zones)} zone(s): {', '.join(z['name'] for z in zones[:6])}"

    def zones(self):
        return [{"id": z["id"], "name": z["name"], "status": z.get("status")} for z in self._pages("/zones")]

    def zone_id(self, zone: str) -> str:
        for z in self.zones():
            if z["name"].lower() == zone.lower():
                return z["id"]
        raise HostingError(f"Cloudflare: no zone {zone} on this account")

    def _list(self, zone):
        zid = self.zone_id(zone)
        return [{"id": r["id"], "zid": zid, "name": r["name"].lower(), "type": r["type"],
                 "value": _txt_unquote(r["content"]) if r["type"] == "TXT" else r["content"],
                 "ttl": r.get("ttl"), "proxied": bool(r.get("proxied"))}
                for r in self._pages(f"/zones/{zid}/dns_records")]

    def _body(self, name, rtype, value, ttl, proxied):
        body: dict = {"type": rtype, "name": name, "content": value, "ttl": ttl if ttl >= 60 else 1}
        if rtype in ("A", "AAAA", "CNAME"):
            body["proxied"] = bool(proxied)
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(content=host or value, priority=int(pri) if host else 10)
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.req("POST", f"/zones/{self.zone_id(zone)}/dns_records", json=self._body(name, rtype, value, ttl, proxied))

    def _update(self, zone, rec, value, ttl, proxied):
        p = rec.get("proxied") if proxied is None else proxied
        self.req("PUT", f"/zones/{rec['zid']}/dns_records/{rec['id']}", json=self._body(rec["name"], rec["type"], value, ttl, p))

    def _remove(self, zone, rec):
        self.req("DELETE", f"/zones/{rec['zid']}/dns_records/{rec['id']}")


class DigitalOcean(Provider):
    label = "DigitalOcean"
    base = "https://api.digitalocean.com/v2"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        a = (self.req("GET", "/account") or {}).get("account") or {}
        return f"account {a.get('email', '?')} ({a.get('status', '?')}); zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["name"], "name": d["name"]} for d in (self.req("GET", "/domains?per_page=200") or {}).get("domains", [])]

    def _list(self, zone):
        out, url = [], f"/domains/{zone}/records?per_page=200"
        while url:
            j = self.req("GET", url)
            for r in j.get("domain_records", []):
                v = r["data"]
                if r["type"] in ("CNAME", "MX", "NS") and not v.endswith(".") and v != "@":
                    v = f"{v}.{zone}" if "." not in v else v
                if r["type"] == "MX":
                    v = f"{r.get('priority', 10)} {v.rstrip('.')}"
                out.append({"id": r["id"], "name": fqdn(r["name"], zone), "type": r["type"], "value": v.rstrip(".") if r["type"] != "TXT" else v, "ttl": r.get("ttl")})
            url = ((j.get("links") or {}).get("pages") or {}).get("next")
        return out

    def _body(self, zone, name, rtype, value, ttl):
        body: dict = {"type": rtype, "name": relative(name, zone) or "@", "data": value, "ttl": max(30, ttl)}
        if rtype in ("CNAME", "NS"):
            body["data"] = value.rstrip(".") + "."
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(data=host.rstrip(".") + ".", priority=int(pri))
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.req("POST", f"/domains/{zone}/records", json=self._body(zone, name, rtype, value, ttl))

    def _update(self, zone, rec, value, ttl, proxied):
        self.req("PUT", f"/domains/{zone}/records/{rec['id']}", json=self._body(zone, rec["name"], rec["type"], value, ttl))

    def _remove(self, zone, rec):
        self.req("DELETE", f"/domains/{zone}/records/{rec['id']}")


class Vultr(Provider):
    label = "Vultr"
    base = "https://api.vultr.com/v2"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'api_key')}"}

    def verify(self):
        a = (self.req("GET", "/account") or {}).get("account") or {}
        return f"account {a.get('email', '?')}; zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["domain"], "name": d["domain"]} for d in (self.req("GET", "/domains?per_page=500") or {}).get("domains", [])]

    def _list(self, zone):
        out = []
        for r in (self.req("GET", f"/domains/{zone}/records?per_page=500") or {}).get("records", []):
            v = r["data"]
            if r["type"] == "MX":
                v = f"{r.get('priority', 10)} {v}"
            out.append({"id": r["id"], "name": fqdn(r["name"], zone), "type": r["type"], "value": _txt_unquote(v) if r["type"] == "TXT" else v, "ttl": r.get("ttl")})
        return out

    def _body(self, zone, name, rtype, value, ttl):
        body: dict = {"type": rtype, "name": relative(name, zone), "data": _txt_quote(value) if rtype == "TXT" else value, "ttl": ttl}
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(data=host, priority=int(pri))
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.req("POST", f"/domains/{zone}/records", json=self._body(zone, name, rtype, value, ttl))

    def _update(self, zone, rec, value, ttl, proxied):
        self.req("PATCH", f"/domains/{zone}/records/{rec['id']}", json=self._body(zone, rec["name"], rec["type"], value, ttl))

    def _remove(self, zone, rec):
        self.req("DELETE", f"/domains/{zone}/records/{rec['id']}")


class Linode(Provider):
    label = "Linode"
    base = "https://api.linode.com/v4"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        p = self.req("GET", "/profile") or {}
        return f"user {p.get('username', '?')}; zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["id"], "name": d["domain"]} for d in (self.req("GET", "/domains?page_size=500") or {}).get("data", [])]

    def _zid(self, zone):
        for z in self.zones():
            if z["name"].lower() == zone.lower():
                return z["id"]
        raise HostingError(f"Linode: no domain {zone}")

    def _list(self, zone):
        zid = self._zid(zone)
        out = []
        for r in (self.req("GET", f"/domains/{zid}/records?page_size=500") or {}).get("data", []):
            v = r["target"]
            if r["type"] == "MX":
                v = f"{r.get('priority', 10)} {v}"
            out.append({"id": r["id"], "zid": zid, "name": fqdn(r["name"], zone), "type": r["type"], "value": v, "ttl": r.get("ttl_sec")})
        return out

    def _body(self, zone, name, rtype, value, ttl):
        body: dict = {"type": rtype, "name": relative(name, zone), "target": value, "ttl_sec": max(300, ttl)}
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(target=host, priority=int(pri))
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.req("POST", f"/domains/{self._zid(zone)}/records", json=self._body(zone, name, rtype, value, ttl))

    def _update(self, zone, rec, value, ttl, proxied):
        self.req("PUT", f"/domains/{rec['zid']}/records/{rec['id']}", json=self._body(zone, rec["name"], rec["type"], value, ttl))

    def _remove(self, zone, rec):
        self.req("DELETE", f"/domains/{rec['zid']}/records/{rec['id']}")


class Porkbun(Provider):
    label = "Porkbun"
    base = "https://api.porkbun.com/api/json/v3"

    def _auth(self, extra: Optional[dict] = None) -> dict:
        return {"apikey": accounts.secret(self.acc, "api_key"), "secretapikey": accounts.secret(self.acc, "secret_key"), **(extra or {})}

    def post(self, path, extra=None):
        j = self.req("POST", path, json=self._auth(extra))
        if (j or {}).get("status") != "SUCCESS":
            raise HostingError(f"Porkbun: {path}: {(j or {}).get('message', j)}")
        return j

    def verify(self):
        j = self.post("/ping")
        return f"keys work (seen from {j.get('yourIp', '?')}); zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["domain"], "name": d["domain"]} for d in self.post("/domain/listAll").get("domains", [])]

    def _list(self, zone):
        out = []
        for r in self.post(f"/dns/retrieve/{zone}").get("records", []):
            v = r["content"]
            if r["type"] == "MX":
                v = f"{r.get('prio') or 10} {v}"
            out.append({"id": r["id"], "name": r["name"].lower(), "type": r["type"], "value": v, "ttl": int(r.get("ttl") or 600)})
        return out

    def _body(self, zone, name, rtype, value, ttl):
        body = {"name": relative(name, zone), "type": rtype, "content": value, "ttl": str(max(600, ttl))}
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(content=host, prio=pri)
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.post(f"/dns/create/{zone}", self._body(zone, name, rtype, value, ttl))

    def _update(self, zone, rec, value, ttl, proxied):
        self.post(f"/dns/edit/{zone}/{rec['id']}", self._body(zone, rec["name"], rec["type"], value, ttl))

    def _remove(self, zone, rec):
        self.post(f"/dns/delete/{zone}/{rec['id']}")


class Netlify(Provider):
    label = "Netlify"
    base = "https://api.netlify.com/api/v1"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        u = self.req("GET", "/user") or {}
        return f"user {u.get('email') or u.get('full_name') or '?'}; DNS zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": z["id"], "name": z["name"]} for z in self.req("GET", "/dns_zones") or []]

    def _zid(self, zone):
        for z in self.zones():
            if z["name"].lower() == zone.lower():
                return z["id"]
        raise HostingError(f"Netlify: no DNS zone {zone}")

    def _list(self, zone):
        zid = self._zid(zone)
        out = []
        for r in self.req("GET", f"/dns_zones/{zid}/dns_records") or []:
            v = r["value"]
            if r["type"] == "MX":
                v = f"{r.get('priority') or 10} {v}"
            out.append({"id": r["id"], "zid": zid, "name": r["hostname"].lower(), "type": r["type"], "value": v, "ttl": r.get("ttl")})
        return out

    def _create(self, zone, name, rtype, value, ttl, proxied):
        body: dict = {"type": rtype, "hostname": name, "value": value, "ttl": ttl}
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(value=host, priority=int(pri))
        self.req("POST", f"/dns_zones/{self._zid(zone)}/dns_records", json=body)

    def _remove(self, zone, rec):
        self.req("DELETE", f"/dns_zones/{rec['zid']}/dns_records/{rec['id']}")


class Vercel(Provider):
    label = "Vercel"
    base = "https://api.vercel.com"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def _q(self, path):
        team = accounts.setting(self.acc, "team_id")
        return f"{path}{'&' if '?' in path else '?'}teamId={team}" if team else path

    def verify(self):
        u = (self.req("GET", self._q("/v2/user")) or {}).get("user") or {}
        return f"user {u.get('username') or u.get('email') or '?'}; domains: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["name"], "name": d["name"]} for d in (self.req("GET", self._q("/v5/domains?limit=100")) or {}).get("domains", [])]

    def _list(self, zone):
        out = []
        for r in (self.req("GET", self._q(f"/v4/domains/{zone}/records?limit=100")) or {}).get("records", []):
            v = r["value"]
            if r["type"] == "MX":
                v = f"{r.get('mxPriority') or 10} {v}"
            out.append({"id": r["id"], "name": fqdn(r["name"], zone), "type": r["type"], "value": v, "ttl": r.get("ttl")})
        return out

    def _body(self, zone, name, rtype, value, ttl):
        body: dict = {"name": relative(name, zone), "type": rtype, "value": value, "ttl": max(60, ttl)}
        if rtype == "MX":
            pri, _, host = value.partition(" ")
            body.update(value=host, mxPriority=int(pri))
        return body

    def _create(self, zone, name, rtype, value, ttl, proxied):
        self.req("POST", self._q(f"/v2/domains/{zone}/records"), json=self._body(zone, name, rtype, value, ttl))

    def _update(self, zone, rec, value, ttl, proxied):
        body = self._body(zone, rec["name"], rec["type"], value, ttl)
        body.pop("type", None)
        self.req("PATCH", self._q(f"/v1/domains/records/{rec['id']}"), json=body)

    def _remove(self, zone, rec):
        self.req("DELETE", self._q(f"/v2/domains/{zone}/records/{rec['id']}"))


# ---- record-set providers ------------------------------------------------------------------------------------- #

class RRsetProvider(Provider):
    """Providers that store whole record sets: set() and delete() are one call each."""

    def _list(self, zone):
        return [{"name": s["name"], "type": s["type"], "value": v, "ttl": s.get("ttl")}
                for s in self.records(zone) for v in s["values"]]


class Desec(RRsetProvider):
    label = "deSEC"
    base = "https://desec.io/api/v1"

    def headers(self):
        return {"Authorization": f"Token {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        a = self.req("GET", "/auth/account/") or {}
        return f"account {a.get('email', '?')}; zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["name"], "name": d["name"]} for d in self.req("GET", "/domains/") or []]

    def records(self, zone):
        out = []
        for s in self.req("GET", f"/domains/{zone}/rrsets/") or []:
            vals = [_txt_unquote(v) if s["type"] == "TXT" else v.rstrip(".") for v in s["records"]]
            out.append({"name": fqdn(s["subname"], zone), "type": s["type"], "values": vals, "ttl": s.get("ttl")})
        return out

    def set(self, zone, name, rtype, values, ttl=300, proxied=None):
        full = fqdn(name, zone)
        rtype = check_record(full, rtype, values)
        recs = [_txt_quote(v) if rtype == "TXT" else (v.rstrip(".") + "." if rtype in ("CNAME", "NS") else v) for v in values]
        # PUT on the collection is an upsert of the listed rrsets (bulk); one rrset here
        self.req("PUT", f"/domains/{zone}/rrsets/", json=[{"subname": relative(full, zone), "type": rtype, "ttl": max(3600, ttl), "records": recs}])
        return {"name": full, "type": rtype, "values": values, "ttl": max(3600, ttl)}

    def delete(self, zone, name, rtype):
        sub = relative(name, zone) or "@"
        self.req("DELETE", f"/domains/{zone}/rrsets/{sub}/{rtype.upper()}/")
        return 1


class Gandi(RRsetProvider):
    label = "Gandi"
    base = "https://api.gandi.net/v5/livedns"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        return f"zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": d["fqdn"], "name": d["fqdn"]} for d in self.req("GET", "/domains") or []]

    def records(self, zone):
        return [{"name": fqdn(s["rrset_name"], zone), "type": s["rrset_type"], "ttl": s.get("rrset_ttl"),
                 "values": [_txt_unquote(v) if s["rrset_type"] == "TXT" else v.rstrip(".") for v in s["rrset_values"]]}
                for s in self.req("GET", f"/domains/{zone}/records") or []]

    def set(self, zone, name, rtype, values, ttl=300, proxied=None):
        full = fqdn(name, zone)
        rtype = check_record(full, rtype, values)
        vals = [_txt_quote(v) if rtype == "TXT" else (v.rstrip(".") + "." if rtype in ("CNAME", "NS") else v) for v in values]
        self.req("PUT", f"/domains/{zone}/records/{relative(full, zone) or '@'}/{rtype}",
                 json={"rrset_values": vals, "rrset_ttl": max(300, ttl)})
        return {"name": full, "type": rtype, "values": values, "ttl": max(300, ttl)}

    def delete(self, zone, name, rtype):
        self.req("DELETE", f"/domains/{zone}/records/{relative(name, zone) or '@'}/{rtype.upper()}")
        return 1


class Hetzner(RRsetProvider):
    """Hetzner's DNS lives in the Cloud API (zones and RRSets) since the DNS Console moved into the Hetzner Console."""
    label = "Hetzner"
    base = "https://api.hetzner.cloud/v1"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        return f"project token works; zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        return [{"id": z["id"], "name": z["name"]} for z in (self.req("GET", "/zones?per_page=50") or {}).get("zones", [])]

    def records(self, zone):
        out, page = [], 1
        while True:
            j = self.req("GET", f"/zones/{quote(zone)}/rrsets?per_page=100&page={page}") or {}
            for s in j.get("rrsets", []):
                out.append({"name": fqdn(s["name"], zone), "type": s["type"], "ttl": s.get("ttl"),
                            "values": [_txt_unquote(r["value"]) if s["type"] == "TXT" else r["value"].rstrip(".") for r in s.get("records", [])]})
            nxt = ((j.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not nxt:
                return out
            page = nxt

    def set(self, zone, name, rtype, values, ttl=300, proxied=None):
        full = fqdn(name, zone)
        rtype = check_record(full, rtype, values)
        sub = relative(full, zone) or "@"
        recs = [{"value": _txt_quote(v) if rtype == "TXT" else (v.rstrip(".") + "." if rtype in ("CNAME", "NS") else v)} for v in values]
        exists = any(s["name"] == full and s["type"] == rtype for s in self.records(zone))
        if exists:
            self.req("POST", f"/zones/{quote(zone)}/rrsets/{quote(sub)}/{rtype}/actions/set_records", json={"records": recs})
            self.req("POST", f"/zones/{quote(zone)}/rrsets/{quote(sub)}/{rtype}/actions/change_ttl", json={"ttl": max(60, ttl)})
        else:
            self.req("POST", f"/zones/{quote(zone)}/rrsets", json={"name": sub, "type": rtype, "ttl": max(60, ttl), "records": recs})
        return {"name": full, "type": rtype, "values": values, "ttl": max(60, ttl)}

    def delete(self, zone, name, rtype):
        self.req("DELETE", f"/zones/{quote(zone)}/rrsets/{quote(relative(name, zone) or '@')}/{rtype.upper()}")
        return 1


class Route53(RRsetProvider):
    label = "Route 53"
    base = "https://route53.amazonaws.com/2013-04-01"
    _NS = "{https://route53.amazonaws.com/doc/2013-04-01/}"

    def _signed(self, method: str, path: str, body: bytes = b"", query: str = "") -> dict:
        """AWS Signature Version 4 for Route 53 (a global service signed in us-east-1)."""
        key_id = accounts.setting(self.acc, "access_key_id")
        secret = accounts.secret(self.acc, "secret_access_key")
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        host = "route53.amazonaws.com"
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join([method, path, query, f"host:{host}", f"x-amz-date:{amz_date}", "", "host;x-amz-date", payload_hash])
        scope = f"{day}/us-east-1/route53/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

        def h(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()
        k = h(h(h(h(("AWS4" + secret).encode(), day), "us-east-1"), "route53"), "aws4_request")
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        return {"x-amz-date": amz_date, "Authorization":
                f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders=host;x-amz-date, Signature={sig}"}

    def call(self, method: str, path: str, body: bytes = b"", query: str = "") -> ET.Element:
        full_path = "/2013-04-01" + path
        headers = self._signed(method, full_path, body, query)
        if body:
            headers["content-type"] = "application/xml"
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=self._transport) as c:
                r = c.request(method, f"https://route53.amazonaws.com{full_path}" + (f"?{query}" if query else ""), content=body, headers=headers)
        except httpx.HTTPError as e:
            raise HostingError(f"Route 53: cannot reach AWS: {e}") from e
        if r.status_code >= 400:
            m = re.search(r"<Message>(.*?)</Message>", r.text)
            raise HostingError(f"Route 53: {method} {path} failed ({r.status_code}): {m.group(1) if m else r.text[:200]}")
        return ET.fromstring(r.content)

    def verify(self):
        return f"keys work; zones: {', '.join(z['name'] for z in self.zones()[:6]) or 'none'}"

    def zones(self):
        x = self.call("GET", "/hostedzone")
        return [{"id": z.findtext(f"{self._NS}Id").rsplit("/", 1)[-1], "name": z.findtext(f"{self._NS}Name").rstrip(".")}
                for z in x.iter(f"{self._NS}HostedZone")]

    def _zid(self, zone):
        for z in self.zones():
            if z["name"].lower() == zone.lower():
                return z["id"]
        raise HostingError(f"Route 53: no hosted zone {zone}")

    def records(self, zone):
        x = self.call("GET", f"/hostedzone/{self._zid(zone)}/rrset")
        out = []
        for s in x.iter(f"{self._NS}ResourceRecordSet"):
            rtype = s.findtext(f"{self._NS}Type")
            vals = [v.text or "" for v in s.iter(f"{self._NS}Value")]
            out.append({"name": (s.findtext(f"{self._NS}Name") or "").rstrip(".").replace("\\052", "*"), "type": rtype,
                        "ttl": int(s.findtext(f"{self._NS}TTL") or 0) or None,
                        "values": [_txt_unquote(v) if rtype == "TXT" else v.rstrip(".") for v in vals]})
        return out

    def _change(self, zone, action, name, rtype, values, ttl):
        vals = "".join(f"<ResourceRecord><Value>{_xml(_txt_quote(v) if rtype == 'TXT' else v)}</Value></ResourceRecord>" for v in values)
        body = (f'<?xml version="1.0" encoding="UTF-8"?><ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
                f"<ChangeBatch><Changes><Change><Action>{action}</Action><ResourceRecordSet><Name>{_xml(name)}.</Name><Type>{rtype}</Type>"
                f"<TTL>{ttl}</TTL><ResourceRecords>{vals}</ResourceRecords></ResourceRecordSet></Change></Changes></ChangeBatch>"
                f"</ChangeResourceRecordSetsRequest>").encode()
        self.call("POST", f"/hostedzone/{self._zid(zone)}/rrset", body)

    def set(self, zone, name, rtype, values, ttl=300, proxied=None):
        full = fqdn(name, zone)
        rtype = check_record(full, rtype, values)
        self._change(zone, "UPSERT", full, rtype, values, ttl)
        return {"name": full, "type": rtype, "values": values, "ttl": ttl}

    def delete(self, zone, name, rtype):
        full = fqdn(name, zone)
        for s in self.records(zone):
            if s["name"] == full and s["type"] == rtype.upper():
                self._change(zone, "DELETE", full, s["type"], s["values"], s["ttl"] or 300)
                return 1
        return 0


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class DuckDNS(Provider):
    """Duck DNS: one A, one AAAA and one TXT per subdomain of duckdns.org, set through its update URL. It cannot list
    records, so records() asks DNS (over HTTPS) what the world currently sees."""
    label = "Duck DNS"
    base = "https://www.duckdns.org"

    def _subs(self) -> list[str]:
        return [s.strip().lower().removesuffix(".duckdns.org") for s in accounts.setting(self.acc, "domains").split(",") if s.strip()]

    def _update(self, sub: str, **params) -> str:
        q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        out = self.req("GET", f"/update?domains={sub}&token={accounts.secret(self.acc, 'token')}&{q}")
        if not str(out).startswith("OK"):
            raise HostingError(f"Duck DNS refused the update of {sub} ({out!r}): check the token and the subdomain")
        return str(out)

    def verify(self):
        subs = self._subs()
        if not subs:
            raise HostingError("Duck DNS: no subdomains set on this account")
        for s in subs:   # an update with no ip only re-asserts the current one; KO means a wrong token or name
            self._update(s, verbose="false", **{"ip": ""})
        return f"token works for {', '.join(s + '.duckdns.org' for s in subs)}"

    def zones(self):
        return [{"id": s, "name": f"{s}.duckdns.org"} for s in self._subs()]

    def records(self, zone):
        from bot.hosting import netinfo
        out = []
        for rtype in ("A", "AAAA", "TXT"):
            vals = netinfo.resolve(zone, rtype)
            if vals:
                out.append({"name": zone, "type": rtype, "values": vals, "ttl": 60})
        return out

    def set(self, zone, name, rtype, values, ttl=60, proxied=None):
        sub = zone.removesuffix(".duckdns.org")
        if fqdn(name, zone) != zone:
            raise HostingError("Duck DNS only has records on the subdomain itself (no names below it)")
        rtype = rtype.upper()
        if rtype == "A":
            self._update(sub, ip=values[0])
        elif rtype == "AAAA":
            self._update(sub, ipv6=values[0])
        elif rtype == "TXT":
            self._update(sub, txt=values[0], verbose="true")
        else:
            raise HostingError("Duck DNS has only A, AAAA and TXT records")
        return {"name": zone, "type": rtype, "values": values[:1], "ttl": 60}

    def delete(self, zone, name, rtype):
        sub = zone.removesuffix(".duckdns.org")
        if rtype.upper() == "TXT":
            self._update(sub, txt="", clear="true")
        else:
            self._update(sub, clear="true")
        return 1


_CLASSES = {"cloudflare": Cloudflare, "digitalocean": DigitalOcean, "vultr": Vultr, "linode": Linode, "porkbun": Porkbun,
            "netlify": Netlify, "vercel": Vercel, "desec": Desec, "gandi": Gandi, "hetzner": Hetzner, "route53": Route53,
            "duckdns": DuckDNS}


def provider(acc: dict | str, transport: Optional[httpx.BaseTransport] = None) -> Provider:
    if isinstance(acc, str):
        acc = accounts.get(acc)
    cls = _CLASSES.get(acc["provider"])
    if not cls:
        raise HostingError(f"{acc['provider']} does not manage DNS here")
    return cls(acc, transport)


def find_zone(host: str) -> tuple[Provider, str]:
    """Which connected account holds `host`'s zone: the longest matching zone over every DNS-capable account."""
    host = host.strip().rstrip(".").lower()
    best: tuple[Optional[Provider], str] = (None, "")
    errors = []
    for a in accounts.listing("dns"):
        try:
            p = provider(a["id"])
            for z in p.zones():
                n = z["name"].lower()
                if (host == n or host.endswith("." + n)) and len(n) > len(best[1]):
                    best = (p, n)
        except HostingError as e:
            errors.append(str(e))
    if not best[0]:
        raise HostingError(f"none of the connected DNS accounts has a zone for {host}"
                           + (f" ({'; '.join(errors)})" if errors else "; connect the account that holds the domain"))
    return best[0], best[1]
