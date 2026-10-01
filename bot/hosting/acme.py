"""Certificates from any ACME certificate authority (RFC 8555): Let's Encrypt, ZeroSSL, Google Trust Services, or a
directory URL a person gives (a company CA, step-ca, Pebble for testing).

    acme.issue(["example.com", "www.example.com"], method="http-01")      # served by the edge on port 80
    acme.issue(["*.example.com", "example.com"], method="dns-01")          # TXT records through a connected DNS account
    acme.renew_due(days=30)                                                # every certificate expiring within 30 days

The account key (EC P-256) lives in `<hosting>/acme/`; a certificate and its key are written to
`<hosting>/certs/<first name>/{cert,key}.pem` (a wildcard as `_wildcard.<zone>`), where the edge picks them up by SNI
without a restart. Creating the CA account means agreeing to the CA's terms of service: issue() refuses unless the
person has agreed (`agree_tos=True`, a box on the Hosting page; the CLI asks).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, hmac as chmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from bot.hosting.store import HostingError, load, root, update

CAS = {
    "letsencrypt": "https://acme-v02.api.letsencrypt.org/directory",
    "letsencrypt-staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "zerossl": "https://acme.zerossl.com/v2/DV90",                  # needs EAB credentials from the ZeroSSL dashboard
    "google": "https://dv.acme-v02.api.pki.goog/directory",         # needs EAB credentials from Google Cloud
}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Client:
    def __init__(self, directory: str, key: ec.EllipticCurvePrivateKey, transport: Optional[httpx.BaseTransport] = None,
                 verify: bool | str = True, log: Callable[[str], None] = lambda m: None):
        self.http = httpx.Client(timeout=30.0, transport=transport, verify=verify, headers={"User-Agent": "ABP-hosting/1"})
        self.dir = self._get(directory).json()
        self.key = key
        self.kid: Optional[str] = None
        self.nonce: Optional[str] = None
        self.log = log

    # -- JWS ----------------------------------------------------------------------------------------------------- #
    def jwk(self) -> dict:
        n = self.key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}

    def thumbprint(self) -> str:
        j = self.jwk()
        canon = json.dumps({"crv": j["crv"], "kty": j["kty"], "x": j["x"], "y": j["y"]}, separators=(",", ":"), sort_keys=True)
        return b64u(hashlib.sha256(canon.encode()).digest())

    def sign(self, protected: dict, payload: Optional[dict | str]) -> dict:
        p = b64u(json.dumps(protected).encode())
        body = "" if payload is None else b64u((payload if isinstance(payload, str) else json.dumps(payload)).encode())
        der = self.key.sign(f"{p}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return {"protected": p, "payload": body, "signature": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}

    def _get(self, url: str) -> httpx.Response:
        try:
            r = self.http.get(url)
        except httpx.HTTPError as e:
            raise HostingError(f"cannot reach the certificate authority at {url}: {e}") from e
        r.raise_for_status()
        return r

    def _nonce(self) -> str:
        if self.nonce:
            n, self.nonce = self.nonce, None
            return n
        return self.http.head(self.dir["newNonce"]).headers["Replay-Nonce"]

    def post(self, url: str, payload: Optional[dict | str], *, use_jwk: bool = False, retry: int = 2) -> httpx.Response:
        protected = {"alg": "ES256", "nonce": self._nonce(), "url": url}
        if use_jwk or not self.kid:
            protected["jwk"] = self.jwk()
        else:
            protected["kid"] = self.kid
        try:
            r = self.http.post(url, json=self.sign(protected, payload), headers={"Content-Type": "application/jose+json"})
        except httpx.HTTPError as e:
            raise HostingError(f"the certificate authority did not answer: {e}") from e
        self.nonce = r.headers.get("Replay-Nonce")
        if r.status_code >= 400:
            try:
                prob = r.json()
            except ValueError:
                prob = {"detail": r.text[:300]}
            if prob.get("type", "").endswith(":badNonce") and retry:
                return self.post(url, payload, use_jwk=use_jwk, retry=retry - 1)
            detail = prob.get("detail", "")
            for sub in prob.get("subproblems") or []:
                detail += f"; {sub.get('identifier', {}).get('value', '')}: {sub.get('detail', '')}"
            raise HostingError(f"the certificate authority refused ({prob.get('type', r.status_code)}): {detail}")
        return r

    # -- account ------------------------------------------------------------------------------------------------- #
    def account(self, email: str = "", eab: Optional[dict] = None, agree_tos: bool = False) -> str:
        if not agree_tos:
            raise HostingError(f"to get certificates, agree to the certificate authority's terms first "
                               f"({(self.dir.get('meta') or {}).get('termsOfService', 'see its website')})")
        payload: dict = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        if (self.dir.get("meta") or {}).get("externalAccountRequired") and not eab:
            raise HostingError("this certificate authority needs External Account Binding: its key ID and HMAC key")
        if eab:
            prot = {"alg": "HS256", "kid": eab["kid"], "url": self.dir["newAccount"]}
            p = b64u(json.dumps(prot).encode())
            body = b64u(json.dumps(self.jwk()).encode())
            h = chmac.HMAC(_b64u_dec(eab["hmac_key"]), hashes.SHA256())
            h.update(f"{p}.{body}".encode())
            payload["externalAccountBinding"] = {"protected": p, "payload": body, "signature": b64u(h.finalize())}
        r = self.post(self.dir["newAccount"], payload, use_jwk=True)
        self.kid = r.headers["Location"]
        return self.kid

    # -- issuance ------------------------------------------------------------------------------------------------ #
    def poll(self, url: str, until: tuple[str, ...], timeout: float = 180.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            j = self.post(url, "").json()       # POST-as-GET
            if j.get("status") in until:
                return j
            if j.get("status") == "invalid":
                errs = [c.get("error", {}).get("detail", "") for c in j.get("challenges", []) if c.get("error")]
                raise HostingError(f"validation failed: {'; '.join(errs) or j.get('error', {}).get('detail', j)}")
            if time.monotonic() > end:
                raise HostingError(f"timed out waiting for {url} to become {'/'.join(until)} (it is {j.get('status')})")
            time.sleep(2.0)


def _account_key(name: str) -> ec.EllipticCurvePrivateKey:
    d = root() / "acme"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"account-{name}.pem"
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return key


def cert_folder(names: list[str]) -> Path:
    first = names[0].lower()
    from bot.hosting.edge import cert_dir
    return cert_dir("_wildcard." + first[2:] if first.startswith("*.") else first)


def issue(names: list[str], *, method: str = "http-01", ca: str = "letsencrypt", email: str = "", agree_tos: bool = False,
          eab: Optional[dict] = None, directory: str = "", transport: Optional[httpx.BaseTransport] = None,
          verify: bool | str = True, log: Callable[[str], None] = lambda m: None) -> dict:
    """Get a certificate for `names` (the first is the main name) and write it where the edge reads it."""
    names = [n.strip().lower().rstrip(".") for n in names if n.strip()]
    if not names:
        raise HostingError("name at least one domain")
    if any(n.startswith("*.") for n in names) and method != "dns-01":
        raise HostingError("a wildcard name needs the dns-01 method (a TXT record), not http-01")
    url = directory or CAS.get(ca)
    if not url:
        raise HostingError(f"unknown certificate authority {ca!r}; one of {', '.join(CAS)} or a directory URL")
    acct_name = hashlib.sha256(url.encode()).hexdigest()[:10]
    c = Client(url, _account_key(acct_name), transport=transport, verify=verify, log=log)
    kids = load("acme-accounts", {})
    c.kid = kids.get(url)
    if not c.kid:
        c.account(email, eab, agree_tos)
        update("acme-accounts", {}, lambda k: k.__setitem__(url, c.kid))
    log(f"ordering a certificate for {', '.join(names)} from {url}")
    r = c.post(c.dir["newOrder"], {"identifiers": [{"type": "dns", "value": n} for n in names]})
    order_url, order = r.headers["Location"], r.json()
    cleanups: list[Callable[[], None]] = []
    try:
        for az_url in order["authorizations"]:
            az = c.post(az_url, "").json()
            if az["status"] == "valid":
                continue
            host = az["identifier"]["value"]
            ch = next((x for x in az["challenges"] if x["type"] == method), None)
            if not ch:
                raise HostingError(f"the CA offers no {method} challenge for {host} (it offers "
                                   f"{', '.join(x['type'] for x in az['challenges'])})")
            key_auth = f"{ch['token']}.{c.thumbprint()}"
            if method == "http-01":
                from bot.hosting.edge import challenge_dir
                f = challenge_dir() / ch["token"]
                f.write_text(key_auth, encoding="utf-8")
                cleanups.append(lambda f=f: f.unlink(missing_ok=True))
                log(f"{host}: answering http://{host}/.well-known/acme-challenge/{ch['token'][:10]}… from the edge")
            elif method == "dns-01":
                from bot.hosting import dns, netinfo
                txt = b64u(hashlib.sha256(key_auth.encode()).digest())
                rec = "_acme-challenge." + host.removeprefix("*.")
                p, zone = dns.find_zone(rec)
                existing = next((s["values"] for s in p.records(zone) if s["name"] == rec and s["type"] == "TXT"), [])
                p.set(zone, rec, "TXT", list(dict.fromkeys(existing + [txt])), ttl=60)
                cleanups.append(lambda p=p, zone=zone, rec=rec: p.delete(zone, rec, "TXT"))
                log(f"{host}: TXT {rec} set through {p.label}; waiting until public resolvers see it")
                end = time.monotonic() + 300
                while txt not in netinfo.resolve(rec, "TXT"):
                    if time.monotonic() > end:
                        raise HostingError(f"the TXT record {rec} did not appear in public DNS within 5 minutes")
                    time.sleep(5)
            else:
                raise HostingError("the method is http-01 or dns-01")
            c.post(ch["url"], {})
            c.poll(az_url, ("valid",))
            log(f"{host}: validated")
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
               .sign(key, hashes.SHA256()))
        c.poll(order_url, ("ready", "valid"))
        c.post(order["finalize"], {"csr": b64u(csr.public_bytes(serialization.Encoding.DER))})
        done = c.poll(order_url, ("valid",))
        pem = c.post(done["certificate"], "").content
    finally:
        for fn in cleanups:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - cleanup is best effort, the certificate matters more
                log(f"cleanup: {e}")
    folder = cert_folder(names)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    (folder / "cert.pem").write_bytes(pem)
    meta = {"names": names, "method": method, "ca": url, "email": email, "issued": int(time.time()), **info(folder / "cert.pem")}
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log(f"certificate saved: valid until {meta['not_after']}")
    return meta


def info(cert_path: Path) -> dict:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    after = cert.not_valid_after_utc
    return {"subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(), "sans": sans,
            "not_after": after.isoformat(), "days_left": (after - _dt.datetime.now(_dt.timezone.utc)).days,
            "self_signed": cert.issuer == cert.subject}


def certificates() -> list[dict]:
    base = root() / "certs"
    out = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if d.name.startswith("_self-signed") or not (d / "cert.pem").exists():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
            out.append({"folder": d.name, **meta, **info(d / "cert.pem"), "managed": bool(meta.get("ca"))})
        except (ValueError, OSError) as e:
            out.append({"folder": d.name, "error": str(e)})
    return out


def renew_due(days: int = 30, log: Callable[[str], None] = lambda m: None) -> list[dict]:
    """Renew every certificate ABP issued that expires within `days` (the account already agreed to the terms)."""
    results = []
    for c in certificates():
        if c.get("managed") and c.get("days_left", 999) <= days:
            try:
                meta = issue(c["names"], method=c.get("method", "http-01"), directory=c["ca"], email=c.get("email", ""),
                             agree_tos=True, log=log)
                results.append({"names": c["names"], "ok": True, "not_after": meta["not_after"]})
            except HostingError as e:
                results.append({"names": c["names"], "ok": False, "error": str(e)})
    return results
