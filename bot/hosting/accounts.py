"""Provider accounts: what a person connects so ABP can manage DNS, tunnels, servers and deployments for them.

An account is a provider, a name, its plain settings, and its secrets. Secrets are sealed with the vault key
(bot/vault.py) before they touch the disk and are never returned by listing() or the API: only `secret(...)`, called by
the code that talks to the provider, opens them. A person types them on the Hosting page, in `abp host account add`
(prompted, not on the command line) or in the TUI; agents can list accounts but never read a secret.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Optional

from bot.hosting.store import HostingError, load, update

# provider -> label, what it can do, its fields: (key, label, secret?, required?)
PROVIDERS: dict[str, dict[str, Any]] = {
    "cloudflare": {"label": "Cloudflare", "caps": ["dns", "tunnel", "pages"], "fields": [
        ("api_token", "API token (Zone.DNS edit; for tunnels Account.Cloudflare Tunnel edit; for Pages Account.Pages edit)", True, True),
        ("account_id", "Account ID (needed for tunnels and Pages; found on the dashboard's right column)", False, False)],
        "help": "https://dash.cloudflare.com/profile/api-tokens"},
    "digitalocean": {"label": "DigitalOcean", "caps": ["dns", "vps"], "fields": [
        ("token", "Personal access token (read + write)", True, True)], "help": "https://cloud.digitalocean.com/account/api/tokens"},
    "hetzner": {"label": "Hetzner Cloud", "caps": ["dns", "vps"], "fields": [
        ("token", "Project API token (read & write)", True, True)], "help": "https://console.hetzner.cloud/ (a project, Security, API tokens)"},
    "vultr": {"label": "Vultr", "caps": ["dns", "vps"], "fields": [
        ("api_key", "API key", True, True)], "help": "https://my.vultr.com/settings/#settingsapi"},
    "linode": {"label": "Akamai / Linode", "caps": ["dns", "vps"], "fields": [
        ("token", "Personal access token (Domains, Linodes: read/write)", True, True)], "help": "https://cloud.linode.com/profile/tokens"},
    "porkbun": {"label": "Porkbun", "caps": ["dns"], "fields": [
        ("api_key", "API key", True, True), ("secret_key", "Secret API key", True, True)],
        "help": "https://porkbun.com/account/api (and turn on API access for the domain)"},
    "desec": {"label": "deSEC", "caps": ["dns"], "fields": [("token", "Token", True, True)], "help": "https://desec.io/tokens"},
    "gandi": {"label": "Gandi", "caps": ["dns"], "fields": [("token", "Personal access token (Domains: manage DNS)", True, True)],
              "help": "https://account.gandi.net/ (Authentication options, Personal access tokens)"},
    "route53": {"label": "AWS Route 53", "caps": ["dns"], "fields": [
        ("access_key_id", "Access key ID", False, True), ("secret_access_key", "Secret access key", True, True)],
        "help": "an IAM user with route53:ListHostedZones, ListResourceRecordSets, ChangeResourceRecordSets"},
    "duckdns": {"label": "Duck DNS", "caps": ["dns"], "fields": [
        ("token", "Token", True, True), ("domains", "Your subdomains, comma separated (e.g. mysite for mysite.duckdns.org)", False, True)],
        "help": "https://www.duckdns.org/"},
    "netlify": {"label": "Netlify", "caps": ["deploy", "dns"], "fields": [
        ("token", "Personal access token", True, True)], "help": "https://app.netlify.com/user/applications#personal-access-tokens"},
    "vercel": {"label": "Vercel", "caps": ["deploy", "dns"], "fields": [
        ("token", "Token", True, True), ("team_id", "Team ID (only for a team's projects)", False, False)],
        "help": "https://vercel.com/account/tokens"},
    "github": {"label": "GitHub Pages", "caps": ["deploy"], "fields": [
        ("token", "Token with Contents and Pages write on the repository", True, True),
        ("repo", "Repository, owner/name", False, True)], "help": "https://github.com/settings/personal-access-tokens"},
    "ssh": {"label": "Server over SSH (VPS or your own machine)", "caps": ["server", "deploy"], "fields": [
        ("host", "Host name or IP", False, True), ("port", "Port (22)", False, False), ("user", "User", False, True),
        ("key_path", "Private key file (empty: your SSH agent / ~/.ssh defaults)", False, False),
        ("web_root", "Where sites live on it (/var/www)", False, False)], "help": ""},
    "ftp": {"label": "FTP / FTPS (shared web hosting)", "caps": ["deploy"], "fields": [
        ("host", "Host", False, True), ("port", "Port (21)", False, False), ("user", "User", False, True),
        ("password", "Password", True, True), ("tls", "Use FTPS (yes/no, default yes)", False, False),
        ("remote_dir", "Folder the site goes in (public_html)", False, False)], "help": ""},
}

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


def _seal(value: str) -> str:
    from bot.vault import seal
    return seal(value)


def _unseal(value: str) -> str:
    from bot.vault import unseal
    return unseal(value)


def _public(acc_id: str, a: dict) -> dict:
    spec = PROVIDERS.get(a["provider"], {})
    return {"id": acc_id, "provider": a["provider"], "label": spec.get("label", a["provider"]), "name": a["name"],
            "caps": spec.get("caps", []), "settings": dict(a.get("settings") or {}),
            "secrets_set": sorted((a.get("secrets") or {}).keys()), "added": a.get("added"),
            "verified": a.get("verified"), "verify_note": a.get("verify_note", "")}


def listing(cap: Optional[str] = None) -> list[dict]:
    accs = load("accounts", {})
    out = [_public(k, v) for k, v in accs.items()]
    if cap:
        out = [a for a in out if cap in a["caps"]]
    return sorted(out, key=lambda a: (a["provider"], a["name"].lower()))


def get(acc_id: str) -> dict:
    accs = load("accounts", {})
    if acc_id not in accs:
        # also by name, so the CLI and agents can say "cloudflare-main"
        hits = [k for k, v in accs.items() if v["name"].lower() == acc_id.lower()]
        if len(hits) != 1:
            raise HostingError(f"no account {acc_id!r}" if not hits else f"several accounts are named {acc_id!r}; use its id")
        acc_id = hits[0]
    return {"id": acc_id, **accs[acc_id]}


def setting(acc: dict, key: str, default: str = "") -> str:
    return str((acc.get("settings") or {}).get(key) or default)


def secret(acc: dict, key: str) -> str:
    sealed = (acc.get("secrets") or {}).get(key)
    return _unseal(sealed) if sealed else ""


def add(provider: str, name: str, values: dict[str, Any]) -> dict:
    spec = PROVIDERS.get(provider)
    if not spec:
        raise HostingError(f"unknown provider {provider!r}; one of: {', '.join(sorted(PROVIDERS))}")
    name = (name or spec["label"]).strip()
    if not _NAME.match(name):
        raise HostingError("a name is 1-64 letters, digits, spaces, dots, dashes or underscores")
    settings, secrets = {}, {}
    for key, _label, is_secret, required in spec["fields"]:
        v = str(values.get(key) or "").strip()
        if required and not v:
            raise HostingError(f"{spec['label']} needs {key}")
        if v:
            (secrets if is_secret else settings)[key] = _seal(v) if is_secret else v
    unknown = set(values) - {f[0] for f in spec["fields"]}
    if unknown:
        raise HostingError(f"{spec['label']} has no field(s) {', '.join(sorted(unknown))}")
    acc_id = f"{provider}-{uuid.uuid4().hex[:8]}"
    rec = {"provider": provider, "name": name, "settings": settings, "secrets": secrets, "added": int(time.time())}

    def put(accs):
        if any(v["name"].lower() == name.lower() for v in accs.values()):
            raise HostingError(f"an account is already named {name!r}")
        accs[acc_id] = rec
    update("accounts", {}, put)
    return _public(acc_id, rec)


def edit(acc_id: str, values: dict[str, Any]) -> dict:
    """Change settings or replace secrets; an empty value for an optional field clears it, for a secret keeps it."""
    acc = get(acc_id)
    spec = PROVIDERS[acc["provider"]]
    fields = {f[0]: f for f in spec["fields"]}

    def put(accs):
        a = accs[acc["id"]]
        for key, v in values.items():
            if key == "name":
                if not _NAME.match(str(v)):
                    raise HostingError("a name is 1-64 letters, digits, spaces, dots, dashes or underscores")
                a["name"] = str(v)
                continue
            if key not in fields:
                raise HostingError(f"{spec['label']} has no field {key}")
            _k, _l, is_secret, required = fields[key]
            v = str(v or "").strip()
            if is_secret:
                if v:
                    a.setdefault("secrets", {})[key] = _seal(v)
            elif v:
                a.setdefault("settings", {})[key] = v
            elif required:
                raise HostingError(f"{key} cannot be empty")
            else:
                a.get("settings", {}).pop(key, None)
        a.pop("verified", None)
        return _public(acc["id"], a)
    return update("accounts", {}, put)


def remove(acc_id: str) -> bool:
    acc = get(acc_id)
    return update("accounts", {}, lambda accs: accs.pop(acc["id"], None) is not None)


def mark_verified(acc_id: str, ok: bool, note: str) -> None:
    def put(accs):
        if acc_id in accs:
            accs[acc_id]["verified"] = int(time.time()) if ok else None
            accs[acc_id]["verify_note"] = note[:300]
    update("accounts", {}, put)


def verify(acc_id: str) -> dict:
    """Ask the provider whether the credentials work (a read-only call), and remember the answer."""
    acc = get(acc_id)
    p = acc["provider"]
    try:
        if p == "ssh":
            from bot.hosting import deploy
            note = deploy.ssh_check(acc)
        elif p == "ftp":
            from bot.hosting import deploy
            note = deploy.ftp_check(acc)
        elif p == "github":
            from bot.hosting import deploy
            note = deploy.check(acc)
        elif "dns" in PROVIDERS[p]["caps"]:
            from bot.hosting import dns
            note = dns.provider(acc).verify()
        else:
            from bot.hosting import vps
            note = vps.provider(acc).verify()
        ok = True
    except HostingError as e:
        ok, note = False, str(e)
    mark_verified(acc["id"], ok, note)
    return {"ok": ok, "note": note}
