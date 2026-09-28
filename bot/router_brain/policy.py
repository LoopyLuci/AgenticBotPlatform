"""How the router thinks, as one document a person can read and edit. Every saved change is a new version, with
who made it and why, and any version can be restored.

    weights     per task class, how much each part of a candidate's score counts:
                quality (how good it is at this kind of task), economy (price), headroom (allowance left),
                reliability (how often calls to it succeed), speed (how fast it answers)
    keywords    extra words that mark a task as coding, hard_reasoning or bulk (added to the built-in ones)
    rules       pin / prefer / avoid / block a model, for every task class or only some, optionally until a date
    learning    whether and how fast it learns from outcomes and feedback, how often it explores, and how long a
                model rests after each kind of failure
    sticky      a bot on `auto` keeps its pick from turn to turn (re-routed when that model starts failing);
                off, it chooses again on every turn
"""
from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any, Optional

from bot.router_brain import store

TASK_CLASSES = ("trivial", "coding", "hard_reasoning", "long_context", "vision", "bulk")
COMPONENTS = ("quality", "economy", "headroom", "reliability", "speed")
RULE_KINDS = ("pin", "prefer", "avoid", "block")
ERROR_KINDS = ("rate_limited", "server", "timeout", "gated", "gone", "auth", "payment", "no_tools", "context", "error")

DEFAULT: dict[str, Any] = {
    "weights": {
        "trivial":        {"quality": 0.10, "economy": 0.40, "headroom": 0.20, "reliability": 0.20, "speed": 0.10},
        "bulk":           {"quality": 0.15, "economy": 0.35, "headroom": 0.25, "reliability": 0.20, "speed": 0.05},
        "coding":         {"quality": 0.45, "economy": 0.10, "headroom": 0.20, "reliability": 0.20, "speed": 0.05},
        "long_context":   {"quality": 0.40, "economy": 0.10, "headroom": 0.25, "reliability": 0.20, "speed": 0.05},
        "hard_reasoning": {"quality": 0.55, "economy": 0.05, "headroom": 0.15, "reliability": 0.20, "speed": 0.05},
        "vision":         {"quality": 0.40, "economy": 0.10, "headroom": 0.25, "reliability": 0.20, "speed": 0.05},
    },
    "keywords": {"coding": [], "hard_reasoning": [], "bulk": [], "trivial": []},
    "rules": [],
    "learning": {
        "enabled": True,              # learn from outcomes and feedback at all
        "explore": 0.10,              # share of automatic picks that try a promising but less proven model
        "half_life_days": 14,         # older outcomes count half as much after this long
        "prior_strength": 4,          # how many outcomes a catalog guess is worth
        "feedback_weight": 2.0,       # one piece of feedback counts as this many outcomes
        "classifier": True,           # let training examples override the keyword classification
        "classifier_min_examples": 3,
        "classifier_confidence": 0.6,
        "similar_example_boost": 0.15,   # a model a similar training example prefers gets up to this much extra
        "failover_for_auto": True,    # a bot on `auto` tries the next pick when its model fails, even with auto_failover off
        "block_after": 3,             # this many gated/gone answers in a row rest a model for the long cooldown
        "cooldown_s": {"rate_limited": 120, "rate_limited_max": 3600, "server": 60, "timeout": 60, "gated": 604800,
                       "gone": 604800, "auth": 86400, "payment": 86400, "no_tools": 604800, "context": 0, "error": 30},
    },
    "sticky": True,
    "record_task_text": True,         # keep the first 240 characters of each routed task (secrets removed)
    "retention_days": 30,
}

_cache: dict[str, Any] = {"key": None, "policy": None, "version": 0}
_cache_lock = threading.Lock()


def _num(value: Any, lo: float, hi: float, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return v


def validate(policy: Any) -> dict[str, Any]:
    """A complete, checked policy: missing parts come from the defaults, anything malformed is refused with a reason."""
    if not isinstance(policy, dict):
        raise ValueError("the policy must be an object")
    out = copy.deepcopy(DEFAULT)
    weights = policy.get("weights", {})
    if not isinstance(weights, dict):
        raise ValueError("weights must map a task class to its weights")
    for cls, w in weights.items():
        if cls not in TASK_CLASSES:
            raise ValueError(f"unknown task class {cls!r}; one of {', '.join(TASK_CLASSES)}")
        if not isinstance(w, dict):
            raise ValueError(f"weights.{cls} must be an object")
        for comp, v in w.items():
            if comp not in COMPONENTS:
                raise ValueError(f"unknown weight {comp!r}; one of {', '.join(COMPONENTS)}")
            out["weights"][cls][comp] = _num(v, 0, 1, f"weights.{cls}.{comp}")
        if sum(out["weights"][cls].values()) <= 0:
            raise ValueError(f"weights.{cls}: at least one weight must be above zero")
    keywords = policy.get("keywords", {})
    if not isinstance(keywords, dict):
        raise ValueError("keywords must map a task class to a list of words")
    for cls, words in keywords.items():
        if cls not in ("coding", "hard_reasoning", "bulk", "trivial"):
            raise ValueError(f"keywords can be added for coding, hard_reasoning, bulk or trivial, not {cls!r}")
        if isinstance(words, str):
            words = [w for w in words.split(",")]
        if not isinstance(words, list):
            raise ValueError(f"keywords.{cls} must be a list")
        out["keywords"][cls] = sorted({str(w).strip().lower() for w in words if str(w).strip()})[:200]
    rules = policy.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    out["rules"] = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError(f"rule {i + 1} must be an object")
        kind = str(r.get("kind", ""))
        if kind not in RULE_KINDS:
            raise ValueError(f"rule {i + 1}: kind must be one of {', '.join(RULE_KINDS)}")
        model = str(r.get("model", "")).strip()
        if "/" not in model:
            raise ValueError(f"rule {i + 1}: model must be provider/model (a provider/* covers every model of it)")
        classes = r.get("classes") or []
        if isinstance(classes, str):
            classes = [c.strip() for c in classes.split(",") if c.strip()]
        bad = [c for c in classes if c not in TASK_CLASSES]
        if bad:
            raise ValueError(f"rule {i + 1}: unknown task class {bad[0]!r}")
        until = r.get("until")
        if until not in (None, ""):
            until = _num(until, 0, 10 ** 11, f"rule {i + 1} until")
        else:
            until = None
        out["rules"].append({"kind": kind, "model": model, "classes": list(classes),
                             "boost": _num(r.get("boost", 0.2), 0, 1, f"rule {i + 1} boost"),
                             "until": until, "note": str(r.get("note", ""))[:300]})
    learning = policy.get("learning", {})
    if not isinstance(learning, dict):
        raise ValueError("learning must be an object")
    L = out["learning"]
    for key, value in learning.items():
        if key not in L:
            raise ValueError(f"unknown learning setting {key!r}")
        if key in ("enabled", "classifier", "failover_for_auto"):
            L[key] = bool(value)
        elif key == "cooldown_s":
            if not isinstance(value, dict):
                raise ValueError("learning.cooldown_s must be an object")
            for k, v in value.items():
                if k not in L["cooldown_s"]:
                    raise ValueError(f"unknown cooldown {k!r}")
                L["cooldown_s"][k] = int(_num(v, 0, 90 * 86400, f"cooldown_s.{k}"))
        else:
            bounds = {"explore": (0, 1), "half_life_days": (0.1, 365), "prior_strength": (0, 100), "feedback_weight": (0, 50),
                      "classifier_min_examples": (1, 1000), "classifier_confidence": (0.34, 1), "similar_example_boost": (0, 1),
                      "block_after": (1, 100)}[key]
            L[key] = _num(value, *bounds, f"learning.{key}")
    for key in ("sticky", "record_task_text"):
        if key in policy:
            out[key] = bool(policy[key])
    if "retention_days" in policy:
        out["retention_days"] = _num(policy["retention_days"], 1, 3650, "retention_days")
    return out


def current() -> dict[str, Any]:
    """The policy in force (the latest version, or the defaults when none was ever saved)."""
    key = str(store.db_path())
    with _cache_lock:
        if _cache["key"] == key and _cache["policy"] is not None and time.monotonic() - _cache.get("at", 0) < 5:
            return copy.deepcopy(_cache["policy"])
    row = None
    try:
        row = store.one("SELECT version, policy FROM policy_versions ORDER BY version DESC LIMIT 1")
    except Exception:  # noqa: BLE001 — an unreadable store means the defaults, never a failed turn
        pass
    policy, version = DEFAULT, 0
    if row:
        try:
            policy, version = validate(json.loads(row["policy"])), int(row["version"])
        except (ValueError, TypeError):
            policy, version = DEFAULT, int(row["version"])
    with _cache_lock:
        _cache.update(key=key, policy=copy.deepcopy(policy), version=version, at=time.monotonic())
    return copy.deepcopy(policy)


def version() -> int:
    current()
    return int(_cache["version"])


def invalidate() -> None:
    with _cache_lock:
        _cache.update(key=None, policy=None)


def diff(old: Any, new: Any, prefix: str = "") -> list[str]:
    """What changed between two policies, one readable line per change."""
    out: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new), key=str):
            out += diff(old.get(k), new.get(k), f"{prefix}{k}.")
        return out
    if old != new:
        out.append(f"{prefix.rstrip('.')}: {json.dumps(old)} -> {json.dumps(new)}")
    return out


def save(policy: Any, *, actor: str = "dashboard", note: str = "") -> dict[str, Any]:
    checked = validate(policy)
    before = current()
    changes = diff(before, checked)
    if not changes:
        return {"version": version(), "changes": []}
    v = store.execute("INSERT INTO policy_versions(ts, actor, note, policy) VALUES (?,?,?,?)",
                      (time.time(), actor, note[:500], json.dumps(checked)))
    invalidate()
    store.event("policy", f"policy v{v} saved by {actor}" + (f": {note}" if note else "") + f" ({len(changes)} change(s))",
                changes=changes[:50])
    return {"version": v, "changes": changes}


def history(limit: int = 50) -> list[dict[str, Any]]:
    found = store.rows("SELECT version, ts, actor, note, policy FROM policy_versions ORDER BY version DESC LIMIT ?", (limit,))
    out = []
    for i, row in enumerate(found):
        older = json.loads(found[i + 1]["policy"]) if i + 1 < len(found) else DEFAULT
        try:
            changes = diff(validate(older), validate(json.loads(row["policy"])))
        except ValueError:
            changes = []
        out.append({"version": row["version"], "ts": row["ts"], "actor": row["actor"], "note": row["note"], "changes": changes})
    return out


def get_version(v: int) -> Optional[dict[str, Any]]:
    if v == 0:
        return copy.deepcopy(DEFAULT)
    row = store.one("SELECT policy FROM policy_versions WHERE version = ?", (v,))
    return validate(json.loads(row["policy"])) if row else None


def rollback(v: int, *, actor: str = "dashboard") -> dict[str, Any]:
    target = get_version(v)
    if target is None:
        raise ValueError(f"no policy version {v}")
    return save(target, actor=actor, note=f"restored version {v}" if v else "restored the defaults")


def active_rules(policy: dict[str, Any], task_class: str, now: Optional[float] = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    return [r for r in policy["rules"] if (not r["classes"] or task_class in r["classes"]) and (not r["until"] or r["until"] > now)]


def rule_matches(rule: dict[str, Any], ref: str) -> bool:
    if rule["model"].endswith("/*"):
        return ref.split("/", 1)[0] == rule["model"][:-2]
    return rule["model"] == ref
