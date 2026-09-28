"""Verifying the signed tokens Google Chat and Microsoft Teams put on every webhook call, and getting the tokens ABP
needs to reply (roadmap P7 channels).

A webhook cannot use the dashboard token (the sender is Google's or Microsoft's service), so the signature on the
request's `Authorization: Bearer <JWT>` is the security boundary. Each token is checked for a valid RS256 signature by a
key the provider publishes, its issuer, its audience (this bot), and its lifetime (5 minutes of clock skew, the figure
both providers document). Published keys are cached for 24 hours and fetched again at once when a token names a key
the cache does not have (providers rotate keys without notice).

Replies need an access token of the bot's own: Google's service-account flow (a JWT assertion signed with the account's
private key) and Microsoft's client-credentials flow. Both tokens are cached until a minute before they expire.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx
import jwt
from cryptography.x509 import load_pem_x509_certificate

KEY_TTL_S = 24 * 3600
LEEWAY_S = 300


class TokenError(Exception):
    pass


class KeySet:
    """Public keys published at a URL: a JWKS document ({"keys": [...]}) or Google's {kid: PEM certificate} map.
    `discovery` names an OpenID configuration whose jwks_uri holds the keys."""

    def __init__(self, url: str, *, discovery: bool = False):
        self.url, self.discovery = url, discovery
        self._keys: dict[str, Any] = {}
        self._endorsements: dict[str, list] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self, client: Optional[httpx.AsyncClient]) -> None:
        own = client is None
        client = client or httpx.AsyncClient(timeout=15)
        try:
            url = self.url
            if self.discovery:
                meta = await client.get(url)
                meta.raise_for_status()
                url = meta.json()["jwks_uri"]
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        finally:
            if own:
                await client.aclose()
        keys, endorsements = {}, {}
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            for jwk in data["keys"]:
                if jwk.get("kty") == "RSA" and jwk.get("kid"):
                    keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
                    endorsements[jwk["kid"]] = list(jwk.get("endorsements") or [])
        elif isinstance(data, dict):                     # Google's x509 map
            for kid, pem in data.items():
                keys[kid] = load_pem_x509_certificate(pem.encode()).public_key()
        if not keys:
            raise TokenError(f"no signing keys at {url}")
        self._keys, self._endorsements, self._fetched = keys, endorsements, time.time()

    async def key(self, kid: str, client: Optional[httpx.AsyncClient] = None):
        async with self._lock:
            if not self._keys or time.time() - self._fetched > KEY_TTL_S:
                await self._fetch(client)
            if kid not in self._keys and time.time() - self._fetched > 60:
                await self._fetch(client)                # a key rotated in since the last fetch
        if kid not in self._keys:
            raise TokenError("the token was signed by a key the provider does not publish")
        return self._keys[kid], self._endorsements.get(kid, [])


async def verify(token: str, keys: KeySet, *, audience: str, issuers: tuple[str, ...],
                 client: Optional[httpx.AsyncClient] = None) -> tuple[dict, list]:
    """(claims, the signing key's endorsements) for a valid token; TokenError otherwise."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError(f"not a JWT: {exc}") from exc
    if header.get("alg") != "RS256":
        raise TokenError(f"unexpected signing algorithm {header.get('alg')!r}")
    key, endorsements = await keys.key(str(header.get("kid") or ""), client)
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience, leeway=LEEWAY_S,
                            options={"require": ["exp", "iss", "aud"]})
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("iss") not in issuers:
        raise TokenError(f"issued by {claims.get('iss')!r}, not {' or '.join(issuers)}")
    return claims, endorsements


def bearer(authorization: str) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenError("no bearer token")
    return token.strip()


class AccessTokens:
    """Access tokens by key, cached until a minute before they expire."""

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}

    async def get(self, key: str, fetch) -> str:
        hit = self._cache.get(key)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
        token, lifetime = await fetch()
        self._cache[key] = (token, time.time() + float(lifetime or 3600))
        return token

    def clear(self) -> None:
        self._cache.clear()


async def google_service_account_token(account: dict, scope: str, client: httpx.AsyncClient) -> tuple[str, int]:
    """OAuth 2.0 for a Google service account: a signed JWT assertion exchanged for an access token."""
    now = int(time.time())
    token_uri = account.get("token_uri") or "https://oauth2.googleapis.com/token"
    assertion = jwt.encode({"iss": account["client_email"], "scope": scope, "aud": token_uri, "iat": now, "exp": now + 3600},
                           account["private_key"], algorithm="RS256", headers={"kid": account.get("private_key_id", "")})
    r = await client.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code >= 300:
        raise TokenError(f"Google refused the service account ({r.status_code}): {r.text[:200]}")
    body = r.json()
    return body["access_token"], int(body.get("expires_in") or 3600)


async def microsoft_client_token(app_id: str, secret: str, tenant: str, scope: str, client: httpx.AsyncClient) -> tuple[str, int]:
    """OAuth 2.0 client credentials against Microsoft Entra ID (tenant "botframework.com" for a multi-tenant bot)."""
    r = await client.post(f"https://login.microsoftonline.com/{tenant or 'botframework.com'}/oauth2/v2.0/token",
                          data={"grant_type": "client_credentials", "client_id": app_id, "client_secret": secret, "scope": scope})
    if r.status_code >= 300:
        raise TokenError(f"Microsoft refused the bot's credentials ({r.status_code}): {r.text[:200]}")
    body = r.json()
    return body["access_token"], int(body.get("expires_in") or 3600)
