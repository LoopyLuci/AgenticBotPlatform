"""Full Tailscale management for ABP: the local `tailscale` CLI (this node's
own preferences, login state, Serve/Funnel, certs, Taildrop, Drive, Tailnet
Lock, exit nodes, diagnostics) plus the Tailscale control-plane API (devices,
ACL policy, DNS, auth keys, tailnet settings, users, webhooks, posture).

Safety rules that hold everywhere in this module:
- Never a shell: every CLI call is an argv list to the `tailscale` binary.
- Every user-supplied value is validated before it reaches an argv slot, and
  no value may begin with "-" (no flag injection).
- Secrets (the API key, auth keys) are read from .env, never returned, never
  logged, and never placed on a command line that shows in the process list
  when the CLI accepts them another way.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from typing import Any, Optional

import httpx

from bot import envfile

_BIN = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
API_BASE = "https://api.tailscale.com/api/v2"

# Preference -> (cli flag, kind). Every `tailscale set` option, so nothing in
# the CLI is unreachable from the GUI/agent.
PREFS: dict[str, tuple[str, str]] = {
    "accept_dns": ("--accept-dns", "bool"),
    "accept_routes": ("--accept-routes", "bool"),
    "advertise_connector": ("--advertise-connector", "bool"),
    "advertise_exit_node": ("--advertise-exit-node", "bool"),
    "advertise_routes": ("--advertise-routes", "cidrs"),
    "auto_update": ("--auto-update", "bool"),
    "exit_node": ("--exit-node", "host"),
    "exit_node_allow_lan_access": ("--exit-node-allow-lan-access", "bool"),
    "hostname": ("--hostname", "hostname"),
    "nickname": ("--nickname", "text"),
    "relay_server_port": ("--relay-server-port", "port_or_empty"),
    "relay_server_static_endpoints": ("--relay-server-static-endpoints", "text"),
    "report_posture": ("--report-posture", "bool"),
    "shields_up": ("--shields-up", "bool"),
    "ssh": ("--ssh", "bool"),
    "unattended": ("--unattended", "bool"),
    "update_check": ("--update-check", "bool"),
    "webclient": ("--webclient", "bool"),
}

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]{0,251})$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61})$")
_TEXT_RE = re.compile(r"^[^\x00-\x1f\"`$;|&<>]{0,200}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~\-/%]{0,200}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")


class TailscaleError(Exception):
    pass


def is_installed() -> bool:
    return bool(shutil.which("tailscale")) or os.path.exists(_BIN)


def _safe(value: Any, kind: str, name: str) -> str:
    s = str(value).strip()
    if kind == "bool":
        if isinstance(value, bool) or s.lower() in ("true", "false"):
            return "true" if (value is True or s.lower() == "true") else "false"
        raise TailscaleError(f"{name} must be true or false")
    if s.startswith("-"):
        raise TailscaleError(f"{name} may not start with '-'")
    if kind == "cidrs":
        if s == "":
            return ""
        try:
            return ",".join(str(ipaddress.ip_network(p.strip(), strict=False)) for p in s.split(","))
        except ValueError as exc:
            raise TailscaleError(f"{name}: {exc}") from exc
    if kind == "host":
        if s == "" or s == "auto:any" or _HOST_RE.match(s):
            return s
        raise TailscaleError(f"{name} is not a valid host, IP, or 'auto:any'")
    if kind == "hostname":
        if name == "hostname" and s == "" or _HOSTNAME_RE.match(s):
            return s
        raise TailscaleError(f"{name} must be a DNS label (letters, digits, hyphen)")
    if kind == "port_or_empty":
        if s == "" or (s.isdigit() and 0 <= int(s) <= 65535):
            return s
        raise TailscaleError(f"{name} must be a port number or empty")
    if kind == "text":
        if _TEXT_RE.match(s):
            return s
        raise TailscaleError(f"{name} contains disallowed characters")
    raise TailscaleError(f"unknown kind {kind}")


def _run(args: list[str], timeout: float = 30.0, stdin: Optional[str] = None) -> tuple[bool, str]:
    if not is_installed():
        return False, "Tailscale is not installed on this machine"
    try:
        proc = subprocess.run(
            [_BIN, *args], capture_output=True, text=True, timeout=timeout, input=stdin,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"tailscale {args[0]} timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def _json(args: list[str], timeout: float = 30.0) -> Any:
    ok, out = _run(args, timeout)
    if not ok:
        raise TailscaleError(out)
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"unexpected output from tailscale {args[0]}: {out[:200]}") from exc


def _result(ok: bool, out: str) -> dict:
    return {"ok": ok, ("output" if ok else "error"): out}


# ---------------------------------------------------------------- read/status
def status() -> dict:
    return _json(["status", "--json"])


def prefs() -> dict:
    return _json(["debug", "prefs"])


def ips() -> dict:
    ok, out = _run(["ip"])
    if not ok:
        raise TailscaleError(out)
    return {"addresses": out.split()}


def whois(address: str) -> dict:
    return _json(["whois", "--json", _safe(address, "host", "address")])


def netcheck() -> dict:
    ok, out = _run(["netcheck", "--format=json"], timeout=60)
    if not ok:
        raise TailscaleError(out)
    return json.loads(out)


def ping(target: str, count: int = 3) -> dict:
    return _result(*_run(["ping", "-c", str(max(1, min(int(count), 10))), "--timeout=5s",
                          _safe(target, "host", "target")], timeout=60))


def exit_nodes() -> dict:
    st = status()
    peers = (st.get("Peer") or {}).values()
    return {"exit_nodes": [
        {"name": p.get("HostName"), "dns": p.get("DNSName"), "ips": p.get("TailscaleIPs"),
         "online": p.get("Online"), "in_use": p.get("ExitNode", False)}
        for p in peers if p.get("ExitNodeOption")
    ]}


def version() -> dict:
    ok, out = _run(["version", "--json"])
    return json.loads(out) if ok and out.startswith("{") else _result(ok, out)


def metrics() -> dict:
    return _result(*_run(["metrics", "print"]))


def dns_status() -> dict:
    return _result(*_run(["dns", "status"]))


# ------------------------------------------------------------------ settings
def set_prefs(changes: dict) -> dict:
    """Apply any subset of PREFS in one `tailscale set` call."""
    if not changes:
        raise TailscaleError("no settings supplied")
    args = ["set"]
    for key, value in changes.items():
        if key not in PREFS:
            raise TailscaleError(f"unknown setting '{key}' (known: {', '.join(sorted(PREFS))})")
        flag, kind = PREFS[key]
        args.append(f"{flag}={_safe(value, kind, key)}")
    if changes.get("advertise_exit_node") or changes.get("ssh"):
        args.append("--accept-risk=lose-ssh")
    return _result(*_run(args, timeout=45))


def up(auth_key: Optional[str] = None, hostname: Optional[str] = None, extra_prefs: Optional[dict] = None) -> dict:
    """Connect. `auth_key`, if given, is passed by stdin-less env-free flag only
    when a key is supplied; otherwise the interactive login URL is returned."""
    args = ["up", "--timeout=30s"]
    if hostname:
        args.append(f"--hostname={_safe(hostname, 'hostname', 'hostname')}")
    for key, value in (extra_prefs or {}).items():
        if key not in PREFS:
            raise TailscaleError(f"unknown setting '{key}'")
        flag, kind = PREFS[key]
        args.append(f"{flag}={_safe(value, kind, key)}")
    if auth_key:
        if not re.match(r"^tskey-[A-Za-z0-9\-]{10,200}$", auth_key):
            raise TailscaleError("that doesn't look like a Tailscale auth key")
        args.append(f"--auth-key={auth_key}")
    return _result(*_run(args, timeout=60))


def down() -> dict:
    return _result(*_run(["down"]))


def login(auth_key: Optional[str] = None) -> dict:
    args = ["login", "--timeout=30s"]
    if auth_key:
        if not re.match(r"^tskey-[A-Za-z0-9\-]{10,200}$", auth_key):
            raise TailscaleError("that doesn't look like a Tailscale auth key")
        args.append(f"--auth-key={auth_key}")
    return _result(*_run(args, timeout=60))


def logout() -> dict:
    return _result(*_run(["logout"]))


def switch_account(account: str) -> dict:
    return _result(*_run(["switch", _safe(account, "text", "account")]))


def list_accounts() -> dict:
    return _result(*_run(["switch", "--list"]))


def update(check_only: bool = True) -> dict:
    return _result(*_run(["update", "--dry-run"] if check_only else ["update", "--yes"], timeout=300))


# ------------------------------------------------------------ Serve / Funnel
def serve_status(funnel: bool = False) -> dict:
    return _json(["funnel" if funnel else "serve", "status", "--json"])


def serve_set(target: str, *, funnel: bool = False, mode: str = "https", port: int = 443,
              path: Optional[str] = None) -> dict:
    """Publish a local target. mode: https | http | tcp | tls-terminated-tcp."""
    if mode not in ("https", "http", "tcp", "tls-terminated-tcp"):
        raise TailscaleError("mode must be https, http, tcp or tls-terminated-tcp")
    if not (1 <= int(port) <= 65535):
        raise TailscaleError("port out of range")
    tgt = str(target).strip()
    if not re.match(r"^(https?\+?i?n?s?e?c?u?r?e?://)?[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~\-/%]*)?$", tgt) or tgt.startswith("-"):
        raise TailscaleError("target must be a port, host:port, or http(s):// URL")
    args = ["funnel" if funnel else "serve", "--bg", "--yes", f"--{mode}={int(port)}"]
    if path:
        if not _PATH_RE.match(path):
            raise TailscaleError("invalid path")
        args.append(f"--set-path={path}")
    args.append(tgt)
    return _result(*_run(args, timeout=60))


def serve_off(*, funnel: bool = False, mode: str = "https", port: int = 443, path: Optional[str] = None) -> dict:
    if mode not in ("https", "http", "tcp", "tls-terminated-tcp"):
        raise TailscaleError("bad mode")
    args = ["funnel" if funnel else "serve", f"--{mode}={int(port)}"]
    if path:
        if not _PATH_RE.match(path):
            raise TailscaleError("invalid path")
        args.append(f"--set-path={path}")
    args.append("off")
    return _result(*_run(args))


def serve_reset(*, funnel: bool = False) -> dict:
    return _result(*_run(["funnel" if funnel else "serve", "reset"]))


def serve_get_config() -> dict:
    return _result(*_run(["serve", "get-config", "--all"]))


def serve_set_config(config_json: str) -> dict:
    try:
        json.loads(config_json)
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"not valid JSON: {exc}") from exc
    return _result(*_run(["serve", "set-config", "--all", "-"], stdin=config_json))


# -------------------------------------------------- certs / files / drive / lock
def cert(domain: str) -> dict:
    """Provision a TLS cert for a tailnet DNS name (files land in the cwd of
    the service's cert dir; the paths are returned, key material never is)."""
    d = _safe(domain, "host", "domain")
    ok, out = _run(["cert", d], timeout=120)
    return _result(ok, out)


def file_send(paths: list[str], target: str) -> dict:
    for p in paths:
        if not os.path.isfile(p):
            raise TailscaleError(f"not a file: {p}")
    return _result(*_run(["file", "cp", *paths, f"{_safe(target, 'host', 'target')}:"], timeout=600))


def file_receive(directory: str) -> dict:
    if not os.path.isdir(directory):
        raise TailscaleError("destination directory does not exist")
    return _result(*_run(["file", "get", directory], timeout=120))


def file_targets() -> dict:
    return _result(*_run(["file", "cp", "--targets"]))


def drive_list() -> dict:
    return _result(*_run(["drive", "list"]))


def drive_share(name: str, path: str) -> dict:
    if not os.path.isdir(path):
        raise TailscaleError("share path must be an existing directory")
    return _result(*_run(["drive", "share", _safe(name, "hostname", "name"), path]))


def drive_unshare(name: str) -> dict:
    return _result(*_run(["drive", "unshare", _safe(name, "hostname", "name")]))


def lock_status() -> dict:
    return _result(*_run(["lock", "status", "--json"]))


def app_connector_routes() -> dict:
    return _result(*_run(["appc-routes"]))


# ------------------------------------------------------------ control-plane API
def _api_key() -> str:
    key = envfile.get_var("TAILSCALE_API_KEY") or ""
    if not key:
        raise TailscaleError(
            "No Tailscale API key set. Create one at login.tailscale.com/admin/settings/keys "
            "and store it with `abp env set TAILSCALE_API_KEY <key>`."
        )
    return key


def _tailnet() -> str:
    return envfile.get_var("TAILSCALE_TAILNET") or "-"


_API_ALLOWED = re.compile(
    r"^/(tailnet/[A-Za-z0-9._\-]+/(devices|acl|acl/validate|acl/preview|dns/[a-z\-/]+|keys(/[A-Za-z0-9\-]+)?|"
    r"settings|users|webhooks(/[A-Za-z0-9\-]+(/[a-z]+)?)?|posture/integrations(/[A-Za-z0-9\-]+)?|"
    r"contacts(/[a-z]+)?|device-invites|user-invites|services(/[A-Za-z0-9\-.]+)?|"
    r"logging/[a-z]+(/stream(/status)?)?|derpmap)"
    r"|device/[A-Za-z0-9\-]+(/(routes|key|tags|ip|authorized|expire|name|device-invites|attributes(/[A-Za-z0-9:_\-]+)?|posture/attributes(/[A-Za-z0-9:_\-]+)?))?"
    r"|user-invites/[A-Za-z0-9\-]+(/[a-z\-]+)?|device-invites/[A-Za-z0-9\-]+(/[a-z\-]+)?)$"
)


def api(method: str, path: str, body: Any = None, params: Optional[dict] = None, raw_text: bool = False) -> Any:
    """Call an allow-listed control-plane endpoint. `path` is relative to
    /api/v2 and may use '-' for the default tailnet."""
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise TailscaleError("bad method")
    path = "/" + path.strip("/")
    if ".." in path or not _API_ALLOWED.match(path):
        raise TailscaleError(f"'{path}' is not a supported Tailscale API endpoint")
    if "/tailnet/-/" in path + "/":
        path = path.replace("/tailnet/-", f"/tailnet/{_tailnet()}", 1)
    headers = {"Authorization": f"Bearer {_api_key()}"}
    if raw_text:
        headers["Accept"] = "application/hujson"
    try:
        resp = httpx.request(method, API_BASE + path, headers=headers, params=params,
                             json=body if not raw_text else None,
                             content=body if raw_text and body else None, timeout=30)
    except httpx.HTTPError as exc:
        raise TailscaleError(f"couldn't reach the Tailscale API: {exc}") from exc
    if resp.status_code >= 400:
        raise TailscaleError(f"Tailscale API {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return {"ok": True}
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


# Typed conveniences over api() — the common tasks by name.
def devices(fields: str = "all") -> Any:
    return api("GET", "/tailnet/-/devices", params={"fields": fields})


def device(device_id: str) -> Any:
    return api("GET", f"/device/{_safe(device_id, 'text', 'device_id')}", params={"fields": "all"})


def device_delete(device_id: str) -> Any:
    return api("DELETE", f"/device/{_safe(device_id, 'text', 'device_id')}")


def device_authorize(device_id: str, authorized: bool = True) -> Any:
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/authorized", {"authorized": bool(authorized)})


def device_expire(device_id: str) -> Any:
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/expire")


def device_set_tags(device_id: str, tags: list[str]) -> Any:
    for t in tags:
        if not re.match(r"^tag:[A-Za-z][A-Za-z0-9\-]{0,60}$", t):
            raise TailscaleError(f"bad tag '{t}' (use tag:name)")
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/tags", {"tags": tags})


def device_set_name(device_id: str, name: str) -> Any:
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/name", {"name": _safe(name, 'hostname', 'name')})


def device_set_key_expiry(device_id: str, disabled: bool) -> Any:
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/key", {"keyExpiryDisabled": bool(disabled)})


def device_routes(device_id: str) -> Any:
    return api("GET", f"/device/{_safe(device_id, 'text', 'device_id')}/routes")


def device_set_routes(device_id: str, routes: list[str]) -> Any:
    clean = [str(ipaddress.ip_network(r, strict=False)) for r in routes]
    return api("POST", f"/device/{_safe(device_id, 'text', 'device_id')}/routes", {"routes": clean})


def acl_get() -> Any:
    return api("GET", "/tailnet/-/acl")


def acl_validate(policy: dict) -> Any:
    return api("POST", "/tailnet/-/acl/validate", policy)


def acl_set(policy: dict) -> Any:
    v = acl_validate(policy)
    if v not in ({}, {"ok": True}, None) and (v.get("message") or v.get("errors")):
        raise TailscaleError(f"policy failed validation: {v}")
    return api("POST", "/tailnet/-/acl", policy)


def dns_get() -> dict:
    return {
        "nameservers": api("GET", "/tailnet/-/dns/nameservers"),
        "search_paths": api("GET", "/tailnet/-/dns/searchpaths"),
        "preferences": api("GET", "/tailnet/-/dns/preferences"),
        "split_dns": api("GET", "/tailnet/-/dns/split-dns"),
    }


def dns_set_nameservers(servers: list[str]) -> Any:
    for s in servers:
        ipaddress.ip_address(s)
    return api("POST", "/tailnet/-/dns/nameservers", {"dns": servers})


def dns_set_search_paths(paths: list[str]) -> Any:
    return api("POST", "/tailnet/-/dns/searchpaths", {"searchPaths": [_safe(p, "host", "search path") for p in paths]})


def dns_set_magic(enabled: bool) -> Any:
    return api("POST", "/tailnet/-/dns/preferences", {"magicDNS": bool(enabled)})


def dns_set_split(mapping: dict) -> Any:
    return api("PATCH", "/tailnet/-/dns/split-dns", mapping)


def keys_list() -> Any:
    return api("GET", "/tailnet/-/keys")


def key_create(*, reusable: bool = False, ephemeral: bool = False, preauthorized: bool = True,
               tags: Optional[list[str]] = None, expiry_seconds: int = 3600, description: str = "") -> Any:
    """Create an auth key. The secret is returned exactly once — same as
    Tailscale's own admin console — and callers must not log it."""
    caps = {"reusable": reusable, "ephemeral": ephemeral, "preauthorized": preauthorized}
    if tags:
        caps["tags"] = tags
    body = {"capabilities": {"devices": {"create": caps}},
            "expirySeconds": max(60, min(int(expiry_seconds), 90 * 86400)),
            "description": _safe(description, "text", "description")}
    return api("POST", "/tailnet/-/keys", body)


def key_delete(key_id: str) -> Any:
    return api("DELETE", f"/tailnet/-/keys/{_safe(key_id, 'text', 'key_id')}")


def settings_get() -> Any:
    return api("GET", "/tailnet/-/settings")


def settings_set(changes: dict) -> Any:
    return api("PATCH", "/tailnet/-/settings", changes)


def users() -> Any:
    return api("GET", "/tailnet/-/users")


def webhooks() -> Any:
    return api("GET", "/tailnet/-/webhooks")
