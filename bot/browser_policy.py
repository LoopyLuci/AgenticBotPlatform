"""What the agent may and may not touch in the user's real browser (docs/browser-extension/DESIGN.md, section 6).

The same rules are pushed to the extension (`effective()` -> `policy.update`) which enforces them locally too, so a
buggy or compromised server still cannot drive the browser onto a bank, a password manager or an admin console.

`classify()` is deliberately conservative: an unknown URL is allowed (the web is mostly harmless and the taint /
approval layers still apply), but anything that looks like money, identity, secrets or a control plane is
`sensitive` - reading it needs an explicit per-site grant and acting on it is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

# Never automated, whatever a user grants: the browser's own pages and other extensions.
BLOCKED_SCHEMES = ("chrome", "edge", "brave", "opera", "vivaldi", "about", "chrome-extension", "moz-extension", "safari-web-extension",
                   "devtools", "view-source", "file", "javascript", "data", "blob", "ftp")

# Host suffix patterns per category. A match means "sensitive": read only with a per-site grant, never acted on.
SENSITIVE_HOSTS: dict[str, tuple[str, ...]] = {
    "banking": ("chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "capitalone.com", "usbank.com", "schwab.com", "fidelity.com",
                "vanguard.com", "hsbc.com", "barclays.co.uk", "lloydsbank.com", "natwest.com", "santander.com", "ing.com", "revolut.com",
                "monzo.com", "n26.com", "wise.com", "americanexpress.com", "discover.com", "tdbank.com", "pnc.com", "ally.com",
                "robinhood.com", "coinbase.com", "binance.com", "kraken.com"),
    "payments": ("paypal.com", "stripe.com", "checkout.stripe.com", "pay.google.com", "payments.google.com", "venmo.com", "cash.app",
                 "squareup.com", "klarna.com", "affirm.com", "wallet.apple.com"),
    "tax_gov": ("irs.gov", "ssa.gov", "id.me", "login.gov", "hmrc.gov.uk", "gov.uk/personal-tax", "cra-arc.gc.ca", "ato.gov.au"),
    "password_manager": ("1password.com", "1password.eu", "bitwarden.com", "lastpass.com", "dashlane.com", "keepersecurity.com", "proton.me/pass",
                         "nordpass.com", "roboform.com"),
    "cloud_console": ("console.aws.amazon.com", "signin.aws.amazon.com", "portal.azure.com", "console.cloud.google.com", "console.cloud.oracle.com",
                      "cloud.digitalocean.com", "dash.cloudflare.com", "admin.google.com", "admin.microsoft.com", "entra.microsoft.com",
                      "app.datadoghq.com", "manage.auth0.com"),
    "identity": ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "appleid.apple.com", "id.apple.com", "auth0.com",
                 "okta.com", "login.okta.com", "account.microsoft.com", "myaccount.google.com"),
    "webmail": ("mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com", "mail.proton.me"),
    "health": ("mychart.com", "patient.info", "myhealth.va.gov"),
}

# Paths that make an otherwise ordinary site sensitive (login, checkout, payment, account security).
SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(login|log-in|signin|sign-in|sso|oauth2?|authorize|2fa|mfa|verify|checkout|payment|payments|billing|wallet|"
    r"account/security|security/settings|password|reset-password|change-password)(/|$|\?|\.)", re.I)

DEFAULT_MAX_TABS = 5
DEFAULT_ACTIONS_PER_MINUTE = 60
DEFAULT_NAVIGATIONS_PER_MINUTE = 20
DEFAULT_MAX_SESSION_MINUTES = 60


@dataclass(frozen=True)
class Verdict:
    allowed: bool                     # may the agent operate on this URL at all
    sensitive: bool                   # reading needs an explicit per-site grant; acting is refused
    category: str = ""
    reason: str = ""


def _host_matches(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    pattern = pattern.lower()
    if "/" in pattern:                                   # host + path prefix, e.g. gov.uk/personal-tax
        return False
    return host == pattern or host.endswith("." + pattern)


def _path_pattern_match(host: str, path: str, pattern: str) -> bool:
    if "/" not in pattern:
        return False
    h, _, p = pattern.partition("/")
    return _host_matches(host, h) and path.lower().startswith("/" + p.lower())


def classify(url: str, *, extra_sensitive: Optional[list[str]] = None, trusted_sites: Optional[list[str]] = None) -> Verdict:
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return Verdict(False, True, "invalid", "not a valid URL")
    scheme = (parts.scheme or "").lower()
    if scheme in BLOCKED_SCHEMES:
        return Verdict(False, True, "browser_internal", f"{scheme}: pages are never automated")
    if scheme not in ("http", "https"):
        return Verdict(False, True, "invalid", f"unsupported scheme {scheme or '(none)'}")
    host = (parts.hostname or "").lower()
    if not host:
        return Verdict(False, True, "invalid", "no host")
    for pattern in extra_sensitive or []:
        if _host_matches(host, pattern) or _path_pattern_match(host, parts.path or "/", pattern):
            return Verdict(True, True, "user_blocklist", f"{host} is on your sensitive-site list")
    for category, patterns in SENSITIVE_HOSTS.items():
        for pattern in patterns:
            if _host_matches(host, pattern) or _path_pattern_match(host, parts.path or "/", pattern):
                return Verdict(True, True, category, f"{host} looks like {category.replace('_', ' ')}")
    trusted = any(_host_matches(host, t.lower().lstrip("*.")) for t in (trusted_sites or []))
    if not trusted and SENSITIVE_PATH_RE.search((parts.path or "/") + ("?" + parts.query if parts.query else "")):
        return Verdict(True, True, "sensitive_page", "this looks like a login, checkout or account-security page")
    return Verdict(True, False)


def _browser_cfg() -> dict:
    try:
        from bot.config import config

        return dict((((config.current.get("native_agent") or {}).get("browser")) or {}))
    except Exception:  # noqa: BLE001 - policy must always be computable
        return {}


def effective() -> dict:
    """The policy sent to the extension in hello.ok and by `policy.update`."""
    cfg = _browser_cfg()
    ext = dict(cfg.get("extension") or {})
    return {
        "blocked_schemes": list(BLOCKED_SCHEMES),
        "sensitive_hosts": {k: list(v) for k, v in SENSITIVE_HOSTS.items()},
        "sensitive_path_regex": SENSITIVE_PATH_RE.pattern,
        "extra_sensitive": list(ext.get("sensitive_sites") or []),
        "sensitive_grants": list(ext.get("sensitive_grants") or []),
        "trusted_sites": list(cfg.get("trusted_sites") or []),
        "max_tabs": int(ext.get("max_tabs") or DEFAULT_MAX_TABS),
        "actions_per_minute": int(ext.get("actions_per_minute") or DEFAULT_ACTIONS_PER_MINUTE),
        "navigations_per_minute": int(ext.get("navigations_per_minute") or DEFAULT_NAVIGATIONS_PER_MINUTE),
        "max_session_minutes": int(ext.get("max_session_minutes") or DEFAULT_MAX_SESSION_MINUTES),
        "capabilities": {"read": True, "interact": True, "navigate": True, "forms": True, "downloads": False, "uploads": False,
                         "eval": False, "history": False, **{k: bool(v) for k, v in (ext.get("capabilities") or {}).items()}},
    }


def check_url(url: str) -> Verdict:
    p = effective()
    return classify(url, extra_sensitive=p["extra_sensitive"], trusted_sites=p["trusted_sites"])


def navigation_verdict(url: str) -> Verdict:
    """May the agent OPEN this URL? Sensitive pages are refused unless the user granted that exact site
    (`native_agent.browser.extension.sensitive_grants`); browser-internal pages are never allowed."""
    v = check_url(url)
    if not v.allowed or not v.sensitive:
        return v
    host = (urlsplit(str(url)).hostname or "").lower()
    if any(_host_matches(host, g.lower()) for g in effective()["sensitive_grants"]):
        return Verdict(True, True, v.category, "allowed by your per-site grant")
    return Verdict(False, True, v.category, v.reason + ". The agent does not open sensitive pages unless you grant that site.")
