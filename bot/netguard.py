"""Sentinel inside ABP: ad-blocking (blocklists + a filtering DNS forwarder) and firewall rule management.

The engine is the Sentinel module's `sentinel_core` package (github.com/LoopyLuci/Sentinel), loaded in-process from
the module's checkout (bot/modules/registry.py finds it). Internally this is "netguard" because `bot/sentinel` is
ABP's own, unrelated self-preservation Sentinel; the page and the module are called Sentinel.

Everything is off until switched on from the Sentinel page, and every setting has a switch there
(config/backends.yaml):

    netguard:
      adblock:
        enabled: false
        listen: 127.0.0.1:5353       # Windows only uses DNS servers on port 53: use 127.0.0.1:53 to filter the machine
        upstream: 1.1.1.1:53
        response: nxdomain           # nxdomain | zero (0.0.0.0 / ::)
        lists: {stevenblack: true}   # sentinel_core.blocklist.CATALOGUE ids
        block: []                    # the owner's own domains to block
        allow: []                    # ...and to always let through (wins over every list)
      system_dns: {enabled: false, dry_run: true}   # point this machine's adapters at the filter
      firewall:
        enabled: false               # off = Sentinel's rules are removed on the next apply
        dry_run: true                # on = "Apply" only shows the plan
        rules: [{name, direction, action, protocol, remote_addresses, remote_ports, local_ports, program, enabled}]

Nothing touches the host's firewall or DNS settings except an explicit Apply from the page with dry run switched off,
and then only Sentinel's own rules (its own firewall group) or the adapters' DNS servers, with a snapshot kept so the
change can be rolled back. Every change is in the audit log and in Sentinel's journal.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("abp.netguard")

MODULE_ID = "sentinel"
RESPONSES = ("nxdomain", "zero")
DEFAULTS: dict[str, Any] = {
    "adblock": {"enabled": False, "listen": "127.0.0.1:5353", "upstream": "1.1.1.1:53", "response": "nxdomain",
                "lists": {}, "block": [], "allow": []},
    "system_dns": {"enabled": False, "dry_run": True},
    "firewall": {"enabled": False, "dry_run": True, "rules": []},
}
MAX_OWN = 2000                       # the owner's own block/allow entries
MAX_RULES = 200


class Unavailable(RuntimeError):
    """The Sentinel module is not installed (or too old) here."""


# ------------------------------------------------------------------ the engine
_core_cache: dict[str, Any] = {}


def core_dir() -> Optional[Path]:
    try:
        from bot.modules import registry
        m = registry.find(MODULE_ID)
        if m is None:
            return None
        d = registry.install_dir(m)
        return d if (d / "sentinel_core" / "__init__.py").is_file() else None
    except Exception:  # noqa: BLE001
        return None


def core():
    """The sentinel_core package, imported from the module checkout. Raises Unavailable with what to do."""
    if "mod" in _core_cache:
        return _core_cache["mod"]
    d = core_dir()
    if d is None:
        raise Unavailable("Sentinel is not installed: install it on the Modules page (github.com/LoopyLuci/Sentinel)")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    import importlib
    mod = importlib.import_module("sentinel_core")
    for sub in ("blocklist", "dns", "firewall", "systemdns"):
        importlib.import_module("sentinel_core." + sub)
    if getattr(mod, "API", 0) != 1:
        raise Unavailable(f"this Sentinel checkout speaks API {getattr(mod, 'API', 0)}, ABP needs 1: update it")
    _core_cache["mod"] = mod
    return mod


def data_dir() -> Path:
    from bot.modules import registry
    try:
        d = registry.data_dir(registry.get(MODULE_ID))
    except Exception:  # noqa: BLE001 - no manifest: same folder name the registry would use
        d = registry.abp_root() / "data" / "module-data" / MODULE_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ settings
def _raw() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("netguard") or {})
    except Exception:  # noqa: BLE001
        return {}


def settings() -> dict:
    raw = _raw()
    out: dict[str, Any] = {}
    for sec, dflt in DEFAULTS.items():
        got = raw.get(sec) if isinstance(raw.get(sec), dict) else {}
        out[sec] = {k: got.get(k, v) for k, v in dflt.items()}
    ab = out["adblock"]
    ab["lists"] = {str(k): bool(v) for k, v in dict(ab["lists"] or {}).items()}
    ab["block"], ab["allow"] = [str(x) for x in ab["block"] or []], [str(x) for x in ab["allow"] or []]
    if ab["response"] not in RESPONSES:
        ab["response"] = "nxdomain"
    out["firewall"]["rules"] = [r for r in out["firewall"]["rules"] or [] if isinstance(r, dict)]
    return out


def hostport(s: str, default_port: int = 53) -> tuple[str, int]:
    s = str(s or "").strip()
    if s.startswith("["):
        host, _, port = s[1:].partition("]")
        port = port.lstrip(":")
    elif s.count(":") == 1:
        host, _, port = s.partition(":")
    else:
        host, port = s, ""
    ip = ipaddress.ip_address(host)              # raises ValueError
    p = int(port) if port else default_port
    if not 1 <= p <= 65535:
        raise ValueError("port out of range")
    return str(ip), p


_BOOLS = {("adblock", "enabled"), ("system_dns", "enabled"), ("system_dns", "dry_run"), ("firewall", "enabled"),
          ("firewall", "dry_run")}


def validate_changes(body: dict) -> dict[tuple[str, ...], Any]:
    """{section: {key: value}} from the page -> config changes. Raises ValueError naming the bad field."""
    if not isinstance(body, dict) or not body:
        raise ValueError("send at least one setting, e.g. {\"adblock\": {\"enabled\": true}}")
    changes: dict[tuple[str, ...], Any] = {}
    for sec, vals in body.items():
        if sec not in DEFAULTS or not isinstance(vals, dict):
            raise ValueError(f"unknown section {sec!r} (adblock, system_dns, firewall)")
        for k, v in vals.items():
            key = (sec, k)
            if k not in DEFAULTS[sec]:
                raise ValueError(f"unknown setting {sec}.{k}")
            if key in _BOOLS:
                if not isinstance(v, bool):
                    raise ValueError(f"{sec}.{k} must be true or false")
            elif key in (("adblock", "listen"), ("adblock", "upstream")):
                hostport(v)
                v = str(v).strip()
            elif key == ("adblock", "response"):
                if v not in RESPONSES:
                    raise ValueError(f"adblock.response must be one of {RESPONSES}")
            elif key == ("adblock", "lists"):
                cat = _catalogue()
                if not isinstance(v, dict) or not all(isinstance(x, bool) for x in v.values()):
                    raise ValueError("adblock.lists maps list ids to true/false")
                if cat and (bad := [x for x in v if x not in cat]):
                    raise ValueError(f"unknown lists: {bad}")
                v = {str(x): bool(y) for x, y in v.items()}
            elif key in (("adblock", "block"), ("adblock", "allow")):
                if not isinstance(v, list) or len(v) > MAX_OWN:
                    raise ValueError(f"adblock.{k} is a list of at most {MAX_OWN} domains")
                v = _domains(v, f"adblock.{k}")
            elif key == ("firewall", "rules"):
                v = validate_rules(v)
            changes[("netguard", sec, k)] = v
    return changes


def _domains(values: list, what: str) -> list[str]:
    out = []
    try:
        norm = core().blocklist.normalize
    except Unavailable:
        norm = None
    for x in values:
        s = str(x).strip().lower().rstrip(".")
        d = norm(s) if norm else (s if s and all(c.isalnum() or c in ".-_*" for c in s) and "." in s else None)
        if not d:
            raise ValueError(f"{what}: {x!r} is not a domain")
        if d not in out:
            out.append(d)
    return out


def validate_rules(rules: Any) -> list[dict]:
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f"firewall.rules is a list of at most {MAX_RULES} rules")
    fw = core().firewall
    out, names = [], set()
    for r in rules:
        try:
            rule = fw.Rule.from_dict(r)
        except fw.RuleError as e:
            raise ValueError(f"rule {r.get('name') if isinstance(r, dict) else r!r}: {e}") from None
        if rule.name.lower() in names:
            raise ValueError(f"two rules are called {rule.name!r}")
        names.add(rule.name.lower())
        out.append({**rule.spec(), "enabled": rule.enabled})
    return out


def save(body: dict, actor: str = "dashboard") -> dict:
    from bot import db
    from bot.config import config
    changes = validate_changes(body)
    config.set_values(changes, actor=actor)
    detail = ", ".join(f"{'.'.join(k[1:])}={_short(v)}" for k, v in changes.items())
    db.log_audit(actor=actor, action="netguard_settings", detail=detail[:500])
    journal("settings", detail)
    service.sync()
    return status()


def _short(v: Any) -> str:
    if isinstance(v, list):
        return f"[{len(v)}]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{'on' if x is True else 'off' if x is False else x}" for k, x in v.items()) + "}"
    return str(v)


# ------------------------------------------------------------------ journal
def journal(kind: str, detail: str, **extra) -> None:
    try:
        with open(data_dir() / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, "detail": detail, **extra}) + "\n")
    except OSError:
        log.warning("could not write the Sentinel journal")


def journal_tail(limit: int = 100) -> list[dict]:
    p = data_dir() / "journal.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[::-1]


# ------------------------------------------------------------------ blocklists
def _catalogue() -> dict:
    try:
        return dict(core().blocklist.CATALOGUE)
    except Unavailable:
        return {}


def _list_path(lid: str) -> Path:
    d = data_dir() / "lists"
    d.mkdir(exist_ok=True)
    return d / f"{lid}.txt"


def lists_view() -> list[dict]:
    on = settings()["adblock"]["lists"]
    out = []
    for lid, meta in _catalogue().items():
        p = _list_path(lid)
        out.append({"id": lid, "name": meta["name"], "url": meta["url"], "enabled": bool(on.get(lid)),
                    "cached": p.is_file(), "bytes": p.stat().st_size if p.is_file() else 0,
                    "updated": p.stat().st_mtime if p.is_file() else None})
    return out


def refresh_lists(only: Optional[str] = None, actor: str = "dashboard") -> dict:
    """Download the switched-on lists (or one list) into the cache and reload the filter."""
    bl = core().blocklist
    cat = bl.CATALOGUE
    on = settings()["adblock"]["lists"]
    ids = [only] if only else [k for k, v in on.items() if v]
    results = {}
    for lid in ids:
        if lid not in cat:
            raise ValueError(f"unknown list {lid!r}")
        try:
            text = bl.fetch(cat[lid]["url"])
            tmp = _list_path(lid).with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(_list_path(lid))
            results[lid] = {"ok": True, "domains": len(bl.parse(text).block)}
        except Exception as e:  # noqa: BLE001 - one bad list never stops the others
            results[lid] = {"ok": False, "error": str(e)[:300]}
    from bot import db
    db.log_audit(actor=actor, action="netguard_lists_refresh", detail=json.dumps(results)[:500])
    journal("lists", "refreshed " + ", ".join(f"{k}: {'ok' if v['ok'] else v['error']}" for k, v in results.items()))
    service.reload_matcher()
    return {"results": results, "lists": lists_view()}


def build_matcher():
    bl = core().blocklist
    s = settings()["adblock"]
    texts = []
    for lid, on in s["lists"].items():
        p = _list_path(lid)
        if on and p.is_file():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
    return bl.build(texts, extra_block=s["block"], extra_allow=s["allow"])


def _matcher_key() -> tuple:
    """What the matcher is built from: a change here means rebuilding it."""
    s = settings()["adblock"]
    files = []
    for lid, on in sorted(s["lists"].items()):
        p = _list_path(lid)
        files.append((lid, on, p.stat().st_mtime if on and p.is_file() else None))
    return tuple(files), tuple(s["block"]), tuple(s["allow"])


# ------------------------------------------------------------------ the running filter
class Service:
    def __init__(self):
        self.filter = None
        self.error: Optional[str] = None
        self.running_with: Optional[tuple] = None
        self.matcher_key: Optional[tuple] = None
        self._lock = threading.RLock()

    def sync(self) -> None:
        """Make the DNS filter match the settings: start, restart (listen/upstream/response changed) or stop."""
        with self._lock:
            ab = settings()["adblock"]
            want = (ab["listen"], ab["upstream"], ab["response"]) if ab["enabled"] else None
            if want == self.running_with and (want is None or (self.filter and self.filter.running)):
                if want is not None and self.matcher_key != _matcher_key():
                    self.reload_matcher()
                return
            self._stop()
            if want is None:
                self.error = None
                return
            try:
                dns = core().dns
                self.matcher_key = _matcher_key()
                self.filter = dns.DnsFilter(build_matcher(), listen=hostport(ab["listen"]),
                                            upstream=hostport(ab["upstream"]), mode=ab["response"])
                self.filter.start()
                self.running_with, self.error = want, None
                journal("dns", f"filter started on {ab['listen']} -> {ab['upstream']} ({len(self.filter.matcher)} domains)")
            except (Unavailable, OSError, ValueError) as e:
                self.filter, self.running_with = None, None
                self.error = f"could not start the DNS filter on {ab['listen']}: {e}"
                journal("dns", self.error)

    def reload_matcher(self) -> None:
        with self._lock:
            if self.filter is not None:
                try:
                    self.matcher_key = _matcher_key()
                    self.filter.set_matcher(build_matcher())
                except Unavailable:
                    pass

    def _stop(self) -> None:
        if self.filter is not None:
            self.filter.stop()
            journal("dns", "filter stopped")
        self.filter, self.running_with = None, None

    def stop(self) -> None:
        with self._lock:
            self._stop()

    def view(self) -> dict:
        f = self.filter
        if f is None or not f.running:
            return {"running": False, "error": self.error}
        addr = f.address
        return {"running": True, "address": f"{addr[0]}:{addr[1]}" if addr else None, "domains": len(f.matcher),
                "stats": dict(f.stats), "error": None}

    def queries(self, limit: int = 200, blocked: Optional[bool] = None) -> list[dict]:
        f = self.filter
        if f is None:
            return []
        rows = list(f.recent)[::-1]
        if blocked is not None:
            rows = [r for r in rows if r["blocked"] is blocked]
        return rows[:limit]

    def check(self, name: str) -> dict:
        m = self.filter.matcher if self.filter is not None else build_matcher()
        blocked, rule = m.check(name)
        return {"name": name, "blocked": blocked, "rule": rule}


service = Service()


def status() -> dict:
    s = settings()
    try:
        mod = core()
        avail = {"available": True, "version": getattr(mod, "__version__", "?"), "dir": str(core_dir())}
    except Unavailable as e:
        avail = {"available": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001 - a broken checkout is shown, never raised
        avail = {"available": False, "reason": f"Sentinel failed to load: {e}"}
    return {"core": avail, "settings": s, "dns": service.view(), "lists": lists_view() if avail["available"] else [],
            "platform": {"windows": os.name == "nt"},
            "system_dns_saved": _read_json("systemdns-saved.json"), "firewall_last": _last_apply_summary()}


# ------------------------------------------------------------------ firewall
def _backend():
    fw = core().firewall
    if fw.WindowsBackend.supported():
        return fw.WindowsBackend(workdir=str(data_dir() / "run"))
    raise Unavailable("firewall management is Windows-only for now (this host has no Windows firewall)")


def desired_rules() -> list:
    fw = core().firewall
    s = settings()["firewall"]
    if not s["enabled"]:
        return []
    return [fw.Rule.from_dict(r) for r in s["rules"]]


def firewall_view() -> dict:
    """Sentinel's rules as configured, as they are on the host, and the exact plan between them."""
    s = settings()["firewall"]
    out: dict[str, Any] = {"enabled": s["enabled"], "dry_run": s["dry_run"], "rules": s["rules"]}
    try:
        res = core().firewall.apply(desired_rules(), _backend(), dry_run=True)
        out.update({"current": res["before"], "changes": res["changes"], "script": res["script"], "error": None})
    except Exception as e:  # noqa: BLE001 - shown on the page
        out.update({"current": [], "changes": [], "script": "", "error": str(e)[:500]})
    out["last"] = _last_apply_summary()
    return out


def _read_json(name: str) -> Any:
    p = data_dir() / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


def _write_json(name: str, value: Any) -> None:
    p = data_dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    tmp.replace(p)


def _last_apply_summary() -> Optional[dict]:
    last = _read_json("firewall-last.json")
    if not last:
        return None
    return {"at": last.get("at"), "ok": last.get("ok"), "applied": last.get("applied"), "error": last.get("error"),
            "can_roll_back": bool(last.get("before") is not None), "changes": len(last.get("changes") or [])}


def _finish(res: dict, what: str, actor: str) -> dict:
    from bot import db
    cmds = [c["command"] for c in res.get("changes", [])]
    if not res["dry_run"] and res.get("changes"):
        _write_json("firewall-last.json", res)
    db.log_audit(actor=actor, action=f"netguard_firewall_{what}",
                 detail=f"dry_run={res['dry_run']} ok={res.get('ok')} changes={len(cmds)}: " + " | ".join(cmds)[:400])
    journal("firewall", f"{what}{' (dry run)' if res['dry_run'] else ''}: {len(cmds)} change(s), ok={res.get('ok')}"
            + (f", error: {res.get('error')}" if res.get("error") else ""), commands=cmds)
    return {k: v for k, v in res.items() if k != "before"}


def firewall_apply(actor: str = "dashboard") -> dict:
    """Run the plan. A dry run (the default setting) only returns it; live, it changes Sentinel's own rules."""
    s = settings()["firewall"]
    res = core().firewall.apply(desired_rules(), _backend(), dry_run=bool(s["dry_run"]))
    return _finish(res, "apply", actor)


def firewall_rollback(actor: str = "dashboard") -> dict:
    last = _read_json("firewall-last.json")
    if not last or last.get("before") is None:
        raise ValueError("nothing to roll back: no live apply has been recorded")
    s = settings()["firewall"]
    res = core().firewall.rollback(last["before"], _backend(), dry_run=bool(s["dry_run"]))
    out = _finish(res, "rollback", actor)
    if not res["dry_run"] and res.get("ok"):
        (data_dir() / "firewall-last.json").unlink(missing_ok=True)
    return out


def upsert_rule(rule: dict, actor: str = "dashboard") -> dict:
    rules = [r for r in settings()["firewall"]["rules"]
             if str(r.get("name", "")).lower() != str(rule.get("name", "")).strip().lower()]
    return save({"firewall": {"rules": [*rules, rule]}}, actor=actor)


def delete_rule(name: str, actor: str = "dashboard") -> dict:
    rules = settings()["firewall"]["rules"]
    left = [r for r in rules if str(r.get("name", "")).lower() != name.lower()]
    if len(left) == len(rules):
        raise KeyError(name)
    return save({"firewall": {"rules": left}}, actor=actor)


def set_rule_enabled(name: str, enabled: bool, actor: str = "dashboard") -> dict:
    rules = settings()["firewall"]["rules"]
    if not any(str(r.get("name", "")).lower() == name.lower() for r in rules):
        raise KeyError(name)
    return save({"firewall": {"rules": [{**r, "enabled": enabled} if str(r.get("name", "")).lower() == name.lower()
                                        else r for r in rules]}}, actor=actor)


# ------------------------------------------------------------------ system DNS
def system_dns_view() -> dict:
    s = settings()
    out: dict[str, Any] = {**s["system_dns"], "saved": _read_json("systemdns-saved.json")}
    try:
        sd = core().systemdns
        if os.name != "nt":
            raise Unavailable("pointing adapters at the filter is Windows-only for now")
        cur = sd.adapters()
        out["adapters"] = cur
        out["plan"] = _system_dns_plan(cur)
        out["error"] = None
    except Exception as e:  # noqa: BLE001
        out.update({"adapters": [], "plan": {"commands": []}, "error": str(e)[:500]})
    return out


def _system_dns_plan(cur: list[dict]) -> dict:
    sd = core().systemdns
    s = settings()
    saved = _read_json("systemdns-saved.json")
    if s["system_dns"]["enabled"]:
        host, port = hostport(s["adblock"]["listen"])
        if port != 53:
            raise ValueError(f"the filter listens on port {port}; Windows only uses DNS servers on port 53 "
                             "(set the listen address to 127.0.0.1:53 first)")
        p = sd.plan_point(cur, address=host)
        p["action"] = "point"
        return p
    if saved:
        p = sd.plan_restore(saved)
        p["action"] = "restore"
        return p
    return {"commands": [], "saved": [], "action": "none"}


def system_dns_apply(actor: str = "dashboard") -> dict:
    from bot import db
    sd = core().systemdns
    s = settings()["system_dns"]
    if os.name != "nt":
        raise Unavailable("pointing adapters at the filter is Windows-only for now")
    p = _system_dns_plan(sd.adapters())
    res = sd.run(p, dry_run=bool(s["dry_run"]), workdir=data_dir() / "run")
    if not res["dry_run"] and res.get("ok"):
        if p["action"] == "point":
            prev = _read_json("systemdns-saved.json") or []
            known = {a["index"] for a in prev}
            _write_json("systemdns-saved.json", prev + [a for a in p["saved"] if a["index"] not in known])
        elif p["action"] == "restore":
            (data_dir() / "systemdns-saved.json").unlink(missing_ok=True)
    db.log_audit(actor=actor, action="netguard_system_dns",
                 detail=f"{p['action']} dry_run={res['dry_run']} ok={res.get('ok')}: " + " | ".join(p["commands"])[:400])
    journal("system_dns", f"{p['action']}{' (dry run)' if res['dry_run'] else ''}: {len(p['commands'])} command(s), "
            f"ok={res.get('ok')}" + (f", error: {res.get('error')}" if res.get("error") else ""), commands=p["commands"])
    return {**res, "action": p["action"]}


# ------------------------------------------------------------------ background task
async def run_forever(stop_event) -> None:
    """Start the filter if the settings say so, and keep it matching them (config edits made outside the page too)."""
    import asyncio
    try:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(service.sync)
            except Exception:  # noqa: BLE001 - a bad sync never kills the loop
                log.exception("Sentinel sync failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
    finally:
        await asyncio.to_thread(service.stop)
