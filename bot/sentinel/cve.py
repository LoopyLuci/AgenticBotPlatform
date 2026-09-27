"""Automatic vulnerability (CVE) detection for everything ABP runs or ships.

Inventories, each checked against OSV.dev (the open vulnerability database
that aggregates the PyPA, GitHub, RustSec, npm and Maven advisories):

- python-env:  every distribution installed in the interpreter running ABP
               right now (what is actually exposed, lockfile or not);
- requirements.lock, desktop-app Cargo.lock, browser-extension
  package-lock.json and the Android version catalog, when they are present
  (a dev checkout), so a vulnerable dependency is caught before it ships.

Findings are de-duplicated across advisory aliases (a PYSEC id and its GHSA
twin are one finding), scored from their CVSS vector, and raised as alerts
(critical at CVSS >= 9.0). Results persist to data/sentinel/cve.json, so the
dashboard shows the last scan even while offline.

fix_python() remediates python-env findings in place: upgrade to the fixed
version, re-import ABP in a fresh interpreter as a smoke test, and roll the
package back if that fails. It runs automatically only when
`sentinel.cve.auto_fix` is on (see config/backends.yaml).
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import time
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from bot.envfile import CODE_ROOT
from bot.sentinel import journal

logger = logging.getLogger("bot.sentinel.cve")

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{id}"
BATCH_SIZE = 500
RESULTS_PATH = journal.SENTINEL_DIR / "cve.json"
CACHE_PATH = journal.SENTINEL_DIR / "osv-cache.json"


# ------------------------------------------------------------ inventories --

def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def inventory_python_env() -> list[dict[str, str]]:
    seen, out = set(), []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        out.append({"ecosystem": "PyPI", "name": name, "version": dist.version, "source": "python-env"})
    return out


def inventory_requirements_lock(path: Path) -> list[dict[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if m:
            out.append({"ecosystem": "PyPI", "name": m.group(1), "version": m.group(2), "source": path.name})
    return out


def inventory_cargo_lock(path: Path) -> list[dict[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [{"ecosystem": "crates.io", "name": p["name"], "version": p["version"], "source": "Cargo.lock"}
            for p in data.get("package", []) if str(p.get("source", "")).startswith("registry+")]


def inventory_npm_lock(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out, seen = [], set()
    for key, meta in (data.get("packages") or {}).items():
        if not key.startswith("node_modules/") or not isinstance(meta, dict) or meta.get("link"):
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        version = meta.get("version")
        if version and (name, version) not in seen:
            seen.add((name, version))
            out.append({"ecosystem": "npm", "name": name, "version": version, "source": "package-lock.json"})
    return out


def inventory_gradle_catalog(path: Path) -> list[dict[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    versions = data.get("versions", {})
    out = []
    for lib in (data.get("libraries") or {}).values():
        if not isinstance(lib, dict):
            continue
        module = lib.get("module") or (f"{lib['group']}:{lib['name']}" if "group" in lib and "name" in lib else None)
        v = lib.get("version")
        if isinstance(v, dict):
            v = versions.get(v.get("ref", "")) if "ref" in v else v.get("strictly") or v.get("require")
        elif v is None and "version.ref" in lib:
            v = versions.get(lib["version.ref"])
        if module and isinstance(v, str):
            out.append({"ecosystem": "Maven", "name": module, "version": v, "source": "libs.versions.toml"})
    return out


def inventory(*, include_checkout: bool = True) -> list[dict[str, str]]:
    items = inventory_python_env()
    if include_checkout:
        sources = [
            (CODE_ROOT / "requirements.lock", inventory_requirements_lock),
            (CODE_ROOT / "desktop-app" / "src-tauri" / "Cargo.lock", inventory_cargo_lock),
            (CODE_ROOT / "browser-extension" / "package-lock.json", inventory_npm_lock),
            (CODE_ROOT / "android-app" / "gradle" / "libs.versions.toml", inventory_gradle_catalog),
        ]
        for path, reader in sources:
            if path.is_file():
                try:
                    items.extend(reader(path))
                except Exception:  # noqa: BLE001 — one unparseable lockfile must not hide the others
                    logger.warning("could not read %s for the CVE scan", path, exc_info=True)
    return items


# ------------------------------------------------------------ severity --

_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62}, "C": {"H": 0.56, "L": 0.22, "N": 0}, "I": {"H": 0.56, "L": 0.22, "N": 0},
    "A": {"H": 0.56, "L": 0.22, "N": 0},
}


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> Optional[float]:
    """CVSS v3.x base score from its vector string (FIRST's formula)."""
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope_changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _W["C"][m["C"]]) * (1 - _W["I"][m["I"]]) * (1 - _W["A"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        exploit = 8.22 * _W["AV"][m["AV"]] * _W["AC"][m["AC"]] * pr * _W["UI"][m["UI"]]
        if impact <= 0:
            return 0.0
        raw = 1.08 * (impact + exploit) if scope_changed else impact + exploit
        return _roundup(min(raw, 10))
    except (KeyError, ValueError, IndexError):
        return None


def _severity(vuln: dict[str, Any]) -> tuple[Optional[float], str]:
    score = None
    for s in vuln.get("severity") or []:
        if str(s.get("type", "")).startswith("CVSS_V3") and s.get("score"):
            score = cvss3_score(s["score"])
            break
    label = str(((vuln.get("database_specific") or {}).get("severity")) or "").upper()
    label = {"MODERATE": "MEDIUM"}.get(label, label)
    if _informational(vuln):
        return None, "INFO"
    if score is not None:
        label = "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW"
    return score, label or "UNKNOWN"


def _informational(vuln: dict[str, Any]) -> Optional[str]:
    """RustSec's non-vulnerability notices ("unmaintained", "unsound", "notice")."""
    for aff in vuln.get("affected") or []:
        info = (aff.get("database_specific") or {}).get("informational")
        if info:
            return str(info)
    return None


def _fixed_versions(vuln: dict[str, Any], name: str, ecosystem: str) -> list[str]:
    out = []
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        if pkg.get("ecosystem") != ecosystem or _norm(pkg.get("name", "")) != _norm(name):
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                # GIT ranges name commits, not releases; only versions are actionable here
                if "fixed" in ev and rng.get("type") != "GIT" and not re.fullmatch(r"[0-9a-f]{40}", ev["fixed"]):
                    out.append(ev["fixed"])
    return sorted(set(out))


# ------------------------------------------------------------ scanning --

def _load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _vuln_details(client: httpx.Client, refs: dict[str, str], cache: dict[str, Any]) -> dict[str, dict]:
    out = {}
    for vid, modified in refs.items():
        hit = cache.get(vid)
        if hit and hit.get("modified") == modified:
            out[vid] = hit
            continue
        r = client.get(OSV_VULN.format(id=vid))
        r.raise_for_status()
        out[vid] = cache[vid] = r.json()
    return out


def scan(items: Optional[list[dict[str, str]]] = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """Queries OSV for every item; returns and persists the scan result.
    Raises httpx.HTTPError when OSV can't be reached (the caller keeps the
    previous result)."""
    items = items if items is not None else inventory()
    started = time.time()
    hits: list[tuple[dict[str, str], dict[str, str]]] = []  # (item, {vuln_id: modified})
    with httpx.Client(timeout=timeout, headers={"User-Agent": "AgenticBotPlatform-sentinel"}) as client:
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i:i + BATCH_SIZE]
            body = {"queries": [{"package": {"name": it["name"], "ecosystem": it["ecosystem"]}, "version": it["version"]}
                                for it in chunk]}
            r = client.post(OSV_BATCH, json=body)
            r.raise_for_status()
            for it, res in zip(chunk, r.json().get("results", [])):
                vulns = {v["id"]: v.get("modified", "") for v in (res or {}).get("vulns") or []}
                if vulns:
                    hits.append((it, vulns))
        cache = _load_cache()
        all_refs: dict[str, str] = {}
        for _it, refs in hits:
            all_refs.update(refs)
        details = _vuln_details(client, all_refs, cache)
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass

    findings = []
    for it, refs in hits:
        groups: list[set[str]] = []
        for vid in refs:
            names = {vid, *(details.get(vid, {}).get("aliases") or [])}
            merged = next((g for g in groups if g & names), None)
            if merged is None:
                groups.append(names)
            else:
                merged |= names
        for g in groups:
            ids = sorted(i for i in g if i in details)
            if not ids:
                continue
            best = max((_severity(details[i]) for i in ids), key=lambda s: (s[0] or 0))
            fixed = sorted({f for i in ids for f in _fixed_versions(details[i], it["name"], it["ecosystem"])})
            cve = next((a for a in sorted(g) if a.startswith("CVE-")), ids[0])
            findings.append({
                **it, "id": cve, "ids": sorted(g), "score": best[0], "severity": best[1],
                "summary": next((details[i].get("summary") for i in ids if details[i].get("summary")), ""),
                "fixed": fixed,
            })
    findings.sort(key=lambda f: (-(f["score"] or 0), f["name"]))
    result = {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
              "packages": len(items), "findings": findings}
    try:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(json.dumps(result, indent=1), encoding="utf-8")
    except OSError:
        pass
    return result


def last_result() -> Optional[dict[str, Any]]:
    try:
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report(result: dict[str, Any]) -> None:
    """Raises one alert per finding; clears alerts for findings that are gone."""
    live = set()
    for f in result["findings"]:
        if f["severity"] == "INFO":  # advisories like "crate is unmaintained": listed, never alerted
            continue
        key = f"cve:{f['ecosystem']}:{_norm(f['name'])}:{f['id']}"
        live.add(key)
        level = "critical" if (f["score"] or 0) >= 9 else "warning"
        fix = f" — fixed in {', '.join(f['fixed'])}" if f["fixed"] else " — no fixed version yet"
        journal.alert(key, f"{f['id']} ({f['severity']}) in {f['name']} {f['version']} [{f['source']}]{fix}: "
                           f"{f['summary']}"[:400], level=level)
    for key in list(journal._last_alert):
        if key.startswith("cve:") and key not in live:
            journal.clear(key)
    real = [f for f in result["findings"] if f["severity"] != "INFO"]
    notices = len(result["findings"]) - len(real)
    journal.record("cve-scan", f"scanned {result['packages']} package(s): {len(real)} vulnerabilit{'y' if len(real) == 1 else 'ies'}"
                   + (f", {notices} informational notice(s)" if notices else ""), level="warning" if real else "info")


# ------------------------------------------------------------ remediation --

def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.+\-]", v))


def _smoke_ok(python: str) -> tuple[bool, str]:
    proc = subprocess.run([python, "-c", "import bot.main, bot.dashboard.server"], cwd=str(CODE_ROOT),
                          capture_output=True, text=True, timeout=180)
    return proc.returncode == 0, (proc.stderr or proc.stdout)[-2000:]


def fix_python(findings: Iterable[dict[str, Any]], *, python: str = sys.executable) -> list[dict[str, Any]]:
    """Upgrades each vulnerable python-env package to its lowest fixed version
    above the installed one, verifying ABP still imports; rolls back on
    failure. Returns one outcome per package."""
    outcomes = []
    per_pkg: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f.get("source") != "python-env" or not f.get("fixed"):
            continue
        newer = [v for v in f["fixed"] if _version_key(v) > _version_key(f["version"])]
        if not newer:
            continue
        target = min(newer, key=_version_key)
        cur = per_pkg.get(_norm(f["name"]))
        if cur is None or _version_key(target) > _version_key(cur["target"]):
            per_pkg[_norm(f["name"])] = {"name": f["name"], "from": f["version"], "target": target}
    for pkg in per_pkg.values():
        spec = f"{pkg['name']}>={pkg['target']}"
        proc = subprocess.run([python, "-m", "pip", "install", "--no-input", "-q", spec],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            outcomes.append({**pkg, "ok": False, "detail": f"pip failed: {proc.stderr[-500:]}"})
            continue
        ok, why = _smoke_ok(python)
        if ok:
            outcomes.append({**pkg, "ok": True, "detail": "upgraded; ABP imports cleanly"})
            journal.record("cve-fix", f"upgraded {pkg['name']} {pkg['from']} -> >= {pkg['target']}", level="warning")
            continue
        subprocess.run([python, "-m", "pip", "install", "--no-input", "-q", f"{pkg['name']}=={pkg['from']}"],
                       capture_output=True, text=True, timeout=600)
        outcomes.append({**pkg, "ok": False, "detail": f"rolled back: ABP failed to import after the upgrade: {why[-300:]}"})
        journal.alert(f"cve-fix:{_norm(pkg['name'])}", f"automatic upgrade of {pkg['name']} broke ABP and was rolled back",
                      level="warning")
    return outcomes
