"""What the router learns, and from what.

From **outcomes** (every model call ABP makes, automatic or not): reliability (decayed success and failure counts),
speed (a moving average of latency), and cooldowns. A failure is sorted into a kind first, because each means something
different: a 429 means "not now" (a short rest that doubles on each repeat), a 403 or 404 means "not for us" (a long
rest; free models are withdrawn or reserved all the time), a 401 means the provider's key is wrong (the whole provider
rests), and a context-length error says nothing bad about the model at all.

From **feedback** on a decision (good choice / bad choice, "this was really a coding task", "it should have used X"):
quality per model and task class, and new training examples.

From **training examples** (text -> task class, text -> preferred model), given by hand or made from feedback: a small
naive Bayes classifier that can override the keyword rules once it is sure, and a similarity boost for the model a
similar example preferred.

Every change is written to the learning log (store.event), so what it learned and why can always be read back.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from typing import Any, Optional

from bot.router_brain import policy as policy_mod
from bot.router_brain import store

_STATUS = re.compile(r"(?:returned|status(?: code)?|HTTP|error)[:\s]+(\d{3})\b", re.I)
_TOKEN = re.compile(r"[a-z][a-z0-9_+#.-]{1,30}")
_STOP = frozenset("the a an and or of to in on for with is are be this that it i you me my we our can could would should "
                  "please what how do does from at by as so if then than into about just".split())


def classify_error(text: str) -> str:
    """The kind of failure an error message describes (see policy.ERROR_KINDS)."""
    t = str(text or "")
    low = t.lower()
    m = _STATUS.search(t)
    code = int(m.group(1)) if m else None
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if code == 429 or "rate limit" in low or "rate-limit" in low or "too many requests" in low:
        return "rate_limited"
    if code == 401 or "invalid api key" in low or "unauthorized" in low or "incorrect api key" in low:
        return "auth"
    if code == 402 or "insufficient credit" in low or "payment required" in low or "insufficient_quota" in low:
        return "payment"
    if ("context" in low and ("length" in low or "window" in low or "too long" in low)) or "maximum context" in low or "too many tokens" in low:
        return "context"
    if ("tool" in low and ("not support" in low or "unsupported" in low)) or "does not support tools" in low:
        return "no_tools"
    if code == 403 or "not allowed" in low or "forbidden" in low:
        return "gated"
    if code == 404 or "no endpoints found" in low or "model not found" in low or "does not exist" in low or "no longer" in low:
        return "gone"
    if code is not None and code >= 500:
        return "server"
    if code in (400, 422):
        return "error"
    return "server" if "connect" in low or "unreachable" in low else "error"


def _decay(row: dict[str, Any], now: float, half_life_days: float) -> float:
    age = max(0.0, now - float(row.get("updated") or now))
    return 0.5 ** (age / (half_life_days * 86400.0))


def _empty(model: str, task_class: str) -> dict[str, Any]:
    return {"model": model, "task_class": task_class, "ok": 0.0, "fail": 0.0, "up": 0.0, "down": 0.0, "latency_ms": None,
            "calls": 0, "streak": 0, "last_error_kind": None, "last_error": None, "last_ok": None, "last_fail": None}


class Knowledge:
    """Everything learned so far, read in two queries: what recommend() consults for every candidate."""

    def __init__(self, *, now: Optional[float] = None):
        self.now = time.time() if now is None else now
        self.policy = policy_mod.current()
        L = self.policy["learning"]
        self.enabled = bool(L["enabled"])
        half_life = float(L["half_life_days"])
        self._stats: dict[tuple[str, str], dict[str, Any]] = {}
        self._rest: dict[str, dict[str, Any]] = {}
        try:
            for row in store.rows("SELECT * FROM model_stats"):
                f = _decay(row, self.now, half_life)
                for k in ("ok", "fail", "up", "down"):
                    row[k] = float(row[k]) * f
                self._stats[(row["model"], row["task_class"])] = row
            for row in store.rows("SELECT * FROM cooldowns WHERE until > ?", (self.now,)):
                self._rest[row["key"]] = row
        except Exception:  # noqa: BLE001 — an unreadable store means "nothing learned yet", never a failed turn
            pass

    def stats(self, model: str, task_class: str = "*") -> dict[str, Any]:
        return self._stats.get((model, task_class)) or _empty(model, task_class)

    def reliability(self, model: str, task_class: str = "*") -> tuple[float, float, float]:
        """(mean, ok, fail): a Beta(ok + 3, fail + 1) mean, so an unknown model starts at 0.75, below a proven one.
        The class's own record counts when it has one; otherwise the model's record over every class."""
        s = self.stats(model, task_class)
        if not s["calls"]:
            s = self.stats(model)
        if not self.enabled:
            return 0.75, s["ok"], s["fail"]
        return (s["ok"] + 3.0) / (s["ok"] + s["fail"] + 4.0), s["ok"], s["fail"]

    def speed(self, model: str) -> tuple[float, Optional[float]]:
        """(0..1, latency ms): 1 for instant, 0.5 at ten seconds per call, 0.5 when unknown."""
        lat = self.stats(model).get("latency_ms")
        if lat is None or not self.enabled:
            return 0.5, None if lat is None else float(lat)
        return 1.0 / (1.0 + float(lat) / 10_000.0), float(lat)

    def quality(self, model: str, task_class: str, prior: float) -> tuple[float, int]:
        """The prior (a measurement or a guess) moved by feedback: (quality, pieces of feedback behind the move)."""
        L = self.policy["learning"]
        s = self.stats(model, task_class)
        w, k = float(L["feedback_weight"]), float(L["prior_strength"])
        up, down = s["up"] * w, s["down"] * w
        if up + down < 1e-6 or not self.enabled:
            return prior, 0
        return (prior * k + up) / (k + up + down), int(round(s["up"] + s["down"]))

    def cooldown(self, ref: str) -> Optional[dict[str, Any]]:
        """The rest `ref` is on right now (its own, or its whole provider's), else None."""
        found = [r for r in (self._rest.get(ref), self._rest.get(f"{ref.split('/', 1)[0]}/*")) if r]
        return max(found, key=lambda r: r["until"]) if found else None


def stats(model: str, task_class: str = "*", *, now: Optional[float] = None) -> dict[str, Any]:
    """Decayed counts for (model, class) as of now. A model never seen has all zeros."""
    now = time.time() if now is None else now
    try:
        row = store.one("SELECT * FROM model_stats WHERE model = ? AND task_class = ?", (model, task_class))
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        return _empty(model, task_class)
    f = _decay(row, now, float(policy_mod.current()["learning"]["half_life_days"]))
    for k in ("ok", "fail", "up", "down"):
        row[k] = float(row[k]) * f
    return row


# ---- cooldowns ------------------------------------------------------------------------------------------------------------
def cooldown(ref: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The rest `ref` is on right now (its own, or its whole provider's), else None."""
    return Knowledge(now=now).cooldown(ref)


def set_cooldown(key: str, seconds: float, *, kind: str, reason: str, manual: bool = False, decision_id: Optional[int] = None) -> dict:
    now = time.time()
    prior = store.one("SELECT strikes, until FROM cooldowns WHERE key = ?", (key,))
    strikes = (int(prior["strikes"]) + 1) if prior and not manual else 1
    until = now + max(0.0, float(seconds))
    store.execute("INSERT INTO cooldowns(key, until, kind, reason, strikes, since, manual) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET until=excluded.until, kind=excluded.kind, reason=excluded.reason, "
                  "strikes=excluded.strikes, since=excluded.since, manual=excluded.manual",
                  (key, until, kind, reason[:500], strikes, now, 1 if manual else 0))
    return {"key": key, "until": until, "strikes": strikes}


def clear_cooldown(key: str, *, actor: str = "dashboard") -> bool:
    found = store.one("SELECT key FROM cooldowns WHERE key = ?", (key,))
    store.execute("DELETE FROM cooldowns WHERE key = ?", (key,))
    if found:
        store.event("cooldown_cleared", f"{key} may be used again ({actor})", model=key)
    return bool(found)


def _rest_for(kind: str, strikes: int, L: dict[str, Any]) -> float:
    cd = L["cooldown_s"]
    if kind == "rate_limited":
        return min(float(cd["rate_limited"]) * (2 ** max(0, strikes - 1)), float(cd["rate_limited_max"]))
    return float(cd.get(kind, cd["error"]))


# ---- observing outcomes ---------------------------------------------------------------------------------------------------
def _bump(model: str, task_class: str, *, ok: bool, latency_ms: Optional[float], kind: Optional[str], error: str, now: float,
          half_life_days: float) -> dict[str, Any]:
    row = store.one("SELECT * FROM model_stats WHERE model = ? AND task_class = ?", (model, task_class))
    f = _decay(row, now, half_life_days) if row else 1.0
    okc = (float(row["ok"]) * f if row else 0.0) + (1.0 if ok else 0.0)
    fail = (float(row["fail"]) * f if row else 0.0) + (0.0 if ok or kind == "context" else 1.0)
    up = float(row["up"]) * f if row else 0.0
    down = float(row["down"]) * f if row else 0.0
    lat = row["latency_ms"] if row else None
    if ok and latency_ms is not None:
        lat = latency_ms if lat is None else 0.7 * float(lat) + 0.3 * float(latency_ms)
    old_streak = int(row["streak"]) if row else 0
    streak = (min(old_streak, 0) - 1) if ok else (max(old_streak, 0) + 1)
    store.execute(
        "INSERT INTO model_stats(model, task_class, ok, fail, up, down, latency_ms, calls, last_ok, last_fail, last_error_kind, last_error, "
        "streak, updated) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(model, task_class) DO UPDATE SET ok=excluded.ok, fail=excluded.fail, "
        "up=excluded.up, down=excluded.down, latency_ms=excluded.latency_ms, calls=model_stats.calls + 1, "
        "last_ok=COALESCE(excluded.last_ok, model_stats.last_ok), last_fail=COALESCE(excluded.last_fail, model_stats.last_fail), "
        "last_error_kind=COALESCE(excluded.last_error_kind, model_stats.last_error_kind), last_error=COALESCE(excluded.last_error, model_stats.last_error), "
        "streak=excluded.streak, updated=excluded.updated",
        (model, task_class, okc, fail, up, down, lat, now if ok else None, None if ok else now, None if ok else kind,
         None if ok else error[:500], streak, now))
    return {"old_streak": old_streak, "streak": streak}


def observe(ref: str, *, ok: bool, task_class: Optional[str] = None, latency_ms: Optional[float] = None, tokens: Optional[int] = None,
            error: str = "", decision_id: Optional[int] = None) -> Optional[str]:
    """Learn from one model call. Returns the failure kind (None on success). Never raises."""
    try:
        return _observe(ref, ok=ok, task_class=task_class, latency_ms=latency_ms, tokens=tokens, error=error, decision_id=decision_id)
    except Exception:  # noqa: BLE001 — learning must never cost a reply
        import logging

        logging.getLogger("bot.router_brain").exception("router: could not record an outcome for %s", ref)
        return None


def _observe(ref, *, ok, task_class, latency_ms, tokens, error, decision_id):
    if not ref or "/" not in ref:
        return None
    pol = policy_mod.current()
    L = pol["learning"]
    now = time.time()
    kind = None if ok else classify_error(error)
    if decision_id:
        d = store.one("SELECT status, task_class FROM decisions WHERE id = ?", (decision_id,))
        if d:
            task_class = task_class or d["task_class"]
            if d["status"] == "pending":
                store.execute("UPDATE decisions SET status=?, error_kind=?, error=?, latency_ms=? WHERE id=?",
                              ("ok" if ok else "failed", kind, None if ok else _clean(error)[:500],
                               int(latency_ms) if latency_ms is not None else None, decision_id))
            store.execute("UPDATE decisions SET calls = calls + 1, tokens = tokens + ? WHERE id = ?", (int(tokens or 0), decision_id))
    if not L["enabled"]:
        return kind
    classes = ["*"] + ([task_class] if task_class and task_class in policy_mod.TASK_CLASSES else [])
    result: dict[str, Any] = {}
    for cls in classes:
        bumped = _bump(ref, cls, ok=ok, latency_ms=latency_ms, kind=kind, error=_clean(error), now=now,
                       half_life_days=float(L["half_life_days"]))
        if cls == "*":
            result = bumped
    if ok:
        prior = store.one("SELECT key, kind, manual FROM cooldowns WHERE key = ?", (ref,))
        if prior and not prior["manual"]:
            store.execute("DELETE FROM cooldowns WHERE key = ? AND until <= ?", (ref, now))
        if result.get("old_streak", 0) >= 2:
            store.event("recovered", f"{ref} answered again after {result['old_streak']} failures in a row", model=ref, decision_id=decision_id)
        return None
    # A failure: rest the model (or its whole provider) for as long as this kind of failure deserves.
    key = f"{ref.split('/', 1)[0]}/*" if kind == "auth" else ref
    existing = store.one("SELECT strikes FROM cooldowns WHERE key = ?", (key,))
    strikes = (int(existing["strikes"]) + 1) if existing else 1
    rest = _rest_for(kind, strikes, L)
    if kind in ("gated", "gone") and result.get("streak", 0) < int(L["block_after"]) and strikes < int(L["block_after"]):
        rest = min(rest, float(L["cooldown_s"]["rate_limited_max"]))   # one 403/404 can be a blip; repeats earn the long rest
    if rest > 0:
        set_cooldown(key, rest, kind=kind, reason=_clean(error)[:300], decision_id=decision_id)
        store.event("cooldown", f"{key} rests for {_human(rest)} after a {kind.replace('_', ' ')} failure"
                    + (f" (strike {strikes})" if strikes > 1 else "") + (f": {_clean(error)[:160]}" if error else ""),
                    model=ref, decision_id=decision_id, failure=kind, seconds=rest, strikes=strikes)
    else:
        store.event("failure", f"{ref} failed ({kind.replace('_', ' ')}); not held against it", model=ref, decision_id=decision_id, failure=kind)
    return kind


def _clean(text: str) -> str:
    try:
        from bot.agent_runtime import secrets_guard

        return secrets_guard.redact(str(text or ""))
    except Exception:  # noqa: BLE001
        return str(text or "")


def _human(seconds: float) -> str:
    s = int(seconds)
    if s >= 86400:
        return f"{s / 86400:g} day(s)"
    if s >= 3600:
        return f"{s / 3600:g} hour(s)"
    if s >= 60:
        return f"{s // 60} minute(s)"
    return f"{s} second(s)"


# ---- feedback ---------------------------------------------------------------------------------------------------------------
def feedback(decision_id: int, *, rating: Optional[int] = None, note: str = "", correct_class: Optional[str] = None,
             preferred_model: Optional[str] = None, actor: str = "dashboard") -> dict[str, Any]:
    d = store.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
    if not d:
        raise KeyError(decision_id)
    if correct_class is not None and correct_class not in policy_mod.TASK_CLASSES:
        raise ValueError(f"unknown task class {correct_class!r}")
    if preferred_model is not None and "/" not in preferred_model:
        raise ValueError("preferred_model must be provider/model")
    if rating is not None and rating not in (-1, 0, 1):
        raise ValueError("rating is -1, 0 or 1")
    now = time.time()
    learned: list[str] = []
    cls = d["task_class"] or "*"
    if rating in (-1, 1) and d["chosen"] and rating != d["rating"]:
        # Undo an earlier opposite rating before applying this one, so flipping a vote does not count twice.
        for c in {"*", cls}:
            row = stats(d["chosen"], c, now=now)
            up, down = row["up"], row["down"]
            if d["rating"] == 1:
                up = max(0.0, up - 1)
            elif d["rating"] == -1:
                down = max(0.0, down - 1)
            up, down = (up + 1, down) if rating == 1 else (up, down + 1)
            store.execute("INSERT INTO model_stats(model, task_class, up, down, updated) VALUES (?,?,?,?,?) ON CONFLICT(model, task_class) "
                          "DO UPDATE SET ok=model_stats.ok * ?, fail=model_stats.fail * ?, up=excluded.up, down=excluded.down, updated=excluded.updated",
                          (d["chosen"], c, up, down, now, *(2 * [_decay_factor(d["chosen"], c, now)])))
        learned.append(f"{d['chosen']} was a {'good' if rating == 1 else 'poor'} choice for a {cls} task")
    text = _task_text(d)
    if correct_class and correct_class != d["task_class"] and text:
        add_example(text, task_class=correct_class, source="feedback", decision_id=decision_id, actor=actor)
        learned.append(f"tasks like this are {correct_class}, not {d['task_class']}")
    if preferred_model and preferred_model != d["chosen"] and text:
        add_example(text, preferred_model=preferred_model, source="feedback", decision_id=decision_id, actor=actor)
        learned.append(f"tasks like this should go to {preferred_model}")
    store.execute("UPDATE decisions SET rating=COALESCE(?, rating), feedback_note=COALESCE(NULLIF(?, ''), feedback_note), "
                  "corrected_class=COALESCE(?, corrected_class), preferred_model=COALESCE(?, preferred_model) WHERE id=?",
                  (rating, note[:500], correct_class, preferred_model, decision_id))
    if learned:
        store.event("feedback", f"decision #{decision_id}: " + "; ".join(learned) + (f" ({note[:120]})" if note else ""),
                    model=d["chosen"], decision_id=decision_id, actor=actor)
    return {"learned": learned}


def _decay_factor(model: str, task_class: str, now: float) -> float:
    row = store.one("SELECT updated FROM model_stats WHERE model = ? AND task_class = ?", (model, task_class))
    return _decay(row, now, float(policy_mod.current()["learning"]["half_life_days"])) if row else 1.0


def _task_text(decision: dict[str, Any]) -> str:
    return str(decision.get("task_excerpt") or "")


# ---- training examples -----------------------------------------------------------------------------------------------------
def add_example(text: str, *, task_class: Optional[str] = None, preferred_model: Optional[str] = None, source: str = "manual",
                decision_id: Optional[int] = None, actor: str = "dashboard") -> int:
    text = str(text or "").strip()
    if not text:
        raise ValueError("an example needs its text")
    if not task_class and not preferred_model:
        raise ValueError("an example teaches a task class, a preferred model, or both")
    if task_class and task_class not in policy_mod.TASK_CLASSES:
        raise ValueError(f"unknown task class {task_class!r}")
    if preferred_model and "/" not in preferred_model:
        raise ValueError("preferred_model must be provider/model")
    eid = store.execute("INSERT INTO examples(ts, text, task_class, preferred_model, source, decision_id) VALUES (?,?,?,?,?,?)",
                        (time.time(), _clean(text)[:2000], task_class, preferred_model, source, decision_id))
    _model_cache.clear()
    if source == "manual":
        store.event("example", f"training example #{eid} added by {actor}: "
                    + ", ".join(x for x in (f"class {task_class}" if task_class else "", f"prefers {preferred_model}" if preferred_model else "") if x),
                    decision_id=decision_id)
    return eid


def delete_example(eid: int, *, actor: str = "dashboard") -> bool:
    found = store.one("SELECT id FROM examples WHERE id = ?", (eid,))
    store.execute("DELETE FROM examples WHERE id = ?", (eid,))
    _model_cache.clear()
    if found:
        store.event("example", f"training example #{eid} removed by {actor}")
    return bool(found)


def examples(limit: int = 500) -> list[dict[str, Any]]:
    return store.rows("SELECT * FROM examples ORDER BY id DESC LIMIT ?", (limit,))


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(str(text or "").lower()) if t not in _STOP]


_model_cache: dict[str, Any] = {}


def _classifier() -> Optional[dict[str, Any]]:
    key = str(store.db_path())
    if _model_cache.get("key") == key and time.monotonic() - _model_cache.get("at", 0) < 30:
        return _model_cache.get("model")
    try:
        found = store.rows("SELECT text, task_class FROM examples WHERE task_class IS NOT NULL")
    except Exception:  # noqa: BLE001
        found = []
    model = None
    if found:
        per_class: dict[str, Counter] = {}
        docs = Counter()
        for r in found:
            per_class.setdefault(r["task_class"], Counter()).update(tokens(r["text"]))
            docs[r["task_class"]] += 1
        vocab = set().union(*per_class.values()) if per_class else set()
        model = {"counts": per_class, "docs": docs, "vocab": len(vocab) or 1, "total": sum(docs.values())}
    _model_cache.update(key=key, model=model, at=time.monotonic())
    return model


def learned_class(text: str) -> Optional[dict[str, Any]]:
    """The classifier's view of `text`: {"task_class", "confidence", "scores"} when it has enough examples, else None."""
    L = policy_mod.current()["learning"]
    model = _classifier()
    if not model or model["total"] < int(L["classifier_min_examples"]):
        return None
    words = tokens(text)
    if not words:
        return None
    logp: dict[str, float] = {}
    for cls, counts in model["counts"].items():
        total = sum(counts.values())
        lp = math.log(model["docs"][cls] / model["total"])
        for w in words:
            lp += math.log((counts.get(w, 0) + 1) / (total + model["vocab"]))
        logp[cls] = lp
    top = max(logp.values())
    exp = {c: math.exp(v - top) for c, v in logp.items()}
    z = sum(exp.values())
    scores = {c: round(v / z, 4) for c, v in sorted(exp.items(), key=lambda kv: -kv[1])}
    best = next(iter(scores))
    # Evidence: at least one word of the task was seen in the winning class's examples.
    seen = any(model["counts"][best].get(w) for w in words)
    return {"task_class": best, "confidence": scores[best], "scores": scores, "evidence": seen, "examples": model["total"]}


def similar_preferences(text: str, *, threshold: float = 0.3) -> list[dict[str, Any]]:
    """Training examples that prefer a model and look like `text` (word overlap), most similar first."""
    words = set(tokens(text))
    if not words:
        return []
    try:
        found = store.rows("SELECT id, text, preferred_model FROM examples WHERE preferred_model IS NOT NULL ORDER BY id DESC LIMIT 2000")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in found:
        other = set(tokens(r["text"]))
        if not other:
            continue
        sim = len(words & other) / len(words | other)
        if sim >= threshold:
            out.append({"example_id": r["id"], "model": r["preferred_model"], "similarity": round(sim, 3)})
    out.sort(key=lambda x: -x["similarity"])
    return out[:10]


# ---- reading it back -----------------------------------------------------------------------------------------------------
def model_board(*, now: Optional[float] = None) -> list[dict[str, Any]]:
    """One row per model the router has seen: reliability with its uncertainty, speed, feedback, rest, share of picks."""
    now = time.time() if now is None else now
    rows = store.rows("SELECT model FROM model_stats WHERE task_class = '*' UNION SELECT chosen FROM decisions WHERE chosen IS NOT NULL "
                      "UNION SELECT key FROM cooldowns WHERE key NOT LIKE '%/*'")
    picks = {r["chosen"]: r["n"] for r in store.rows(
        "SELECT chosen, COUNT(*) AS n FROM decisions WHERE mode != 'advise' AND ts > ? GROUP BY chosen", (now - 7 * 86400,))}
    total = sum(picks.values()) or 1
    k = Knowledge(now=now)
    out = []
    for r in rows:
        ref = r["model"]
        if not ref:
            continue
        s = k.stats(ref)
        a, b = s["ok"] + 3.0, s["fail"] + 1.0
        mean = a / (a + b)
        sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        cd = k.cooldown(ref)
        per_class = {}
        for c in policy_mod.TASK_CLASSES:
            sc = k.stats(ref, c)
            if sc["calls"]:
                per_class[c] = {"reliability": round((sc["ok"] + 3) / (sc["ok"] + sc["fail"] + 4), 3), "calls": sc["calls"],
                                "up": round(sc["up"], 2), "down": round(sc["down"], 2)}
        out.append({"model": ref, "reliability": round(mean, 3), "low": round(max(0, mean - 1.64 * sd), 3), "high": round(min(1, mean + 1.64 * sd), 3),
                    "ok": round(s["ok"], 2), "fail": round(s["fail"], 2), "calls": s["calls"], "latency_ms": s["latency_ms"],
                    "up": round(s["up"], 2), "down": round(s["down"], 2), "streak": s["streak"], "last_error_kind": s["last_error_kind"],
                    "last_error": s["last_error"], "last_ok": s["last_ok"], "last_fail": s["last_fail"],
                    "cooldown": cd, "picks_7d": picks.get(ref, 0), "share_7d": round(picks.get(ref, 0) / total, 3), "classes": per_class})
    out.sort(key=lambda x: (-x["picks_7d"], -x["calls"], x["model"]))
    return out


def overview(hours: int = 24, *, now: Optional[float] = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - hours * 3600
    counts = {r["status"]: r["n"] for r in store.rows(
        "SELECT status, COUNT(*) AS n FROM decisions WHERE ts > ? AND mode != 'advise' GROUP BY status", (since,))}
    decided = sum(counts.values())
    explored = (store.one("SELECT COUNT(*) AS n FROM decisions WHERE ts > ? AND explored = 1", (since,)) or {}).get("n", 0)
    bucket = 3600 if hours <= 48 else 86400
    series: dict[int, dict[str, int]] = {}
    for r in store.rows("SELECT CAST(ts / ? AS INTEGER) AS b, status, COUNT(*) AS n FROM decisions WHERE ts > ? AND mode != 'advise' "
                        "GROUP BY b, status", (bucket, since)):
        series.setdefault(int(r["b"]), {})[r["status"]] = r["n"]
    first = int(since // bucket) + 1
    timeline = [{"t": b * bucket, **{k: series.get(b, {}).get(k, 0) for k in ("ok", "failed", "pending")}} for b in range(first, int(now // bucket) + 1)]
    heat: dict[str, dict[str, dict[str, int]]] = {}
    for r in store.rows("SELECT chosen, task_class, status, COUNT(*) AS n FROM decisions WHERE ts > ? AND mode != 'advise' AND chosen IS NOT NULL "
                        "GROUP BY chosen, task_class, status", (now - 7 * 86400,)):
        cell = heat.setdefault(r["chosen"], {}).setdefault(r["task_class"] or "?", {"ok": 0, "failed": 0, "pending": 0})
        cell[r["status"]] = cell.get(r["status"], 0) + r["n"]
    classes = {r["task_class"]: r["n"] for r in store.rows(
        "SELECT task_class, COUNT(*) AS n FROM decisions WHERE ts > ? AND mode != 'advise' GROUP BY task_class", (since,))}
    resting = store.rows("SELECT * FROM cooldowns WHERE until > ? ORDER BY until", (now,))
    return {"hours": hours, "decisions": decided, "ok": counts.get("ok", 0), "failed": counts.get("failed", 0), "pending": counts.get("pending", 0),
            "success_rate": round(counts.get("ok", 0) / max(1, counts.get("ok", 0) + counts.get("failed", 0)), 3) if decided else None,
            "explored": explored, "bucket_s": bucket, "timeline": timeline, "heatmap": heat, "classes": classes, "resting": resting,
            "policy_version": policy_mod.version(), "examples": (store.one("SELECT COUNT(*) AS n FROM examples") or {}).get("n", 0),
            "learning": policy_mod.current()["learning"]["enabled"]}


def events(limit: int = 200, *, kind: Optional[str] = None, model: Optional[str] = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM events WHERE 1=1", []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if model:
        sql += " AND model = ?"
        params.append(model)
    out = store.rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
    for e in out:
        e["data"] = json.loads(e["data"]) if e.get("data") else None
    return out


def reset_model(ref: str, *, actor: str = "dashboard") -> None:
    store.execute("DELETE FROM model_stats WHERE model = ?", (ref,))
    store.execute("DELETE FROM cooldowns WHERE key = ?", (ref,))
    store.event("reset", f"everything learned about {ref} was forgotten ({actor})", model=ref)
