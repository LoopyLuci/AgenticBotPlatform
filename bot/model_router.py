"""The model router: which of your models suits this task, why, and what it learned from last time.

    recommend("fix the failing test in auth.py", candidates=[...]) -> (Classification, [Recommendation, ...], skipped)
    route(task, mode="auto", ...)                                   -> Decision (recorded, with every reason)

`recommend()` advises (the agent's `suggest_model` tool, `/route` in chat, the dashboard). `route()` is what a bot on
`model: auto` actually runs: the same thinking, plus a little exploration, recorded in data/agent/router.db so every
choice can be read back, judged and corrected on the dashboard's Router page. How it thinks is an editable, versioned
policy (bot/router_brain/policy.py); what it learns is in bot/router_brain/learn.py.

1. **Classify the task**: `trivial`, `coding`, `hard_reasoning`, `long_context`, `vision` or `bulk`, from its words (the
   built-in patterns plus the policy's extra keywords). Once there are enough training examples, a small classifier
   trained on them overrides the keywords when it is sure.
2. **Filter the candidates** to those that can do it: tool calling for agentic work, image input for vision, a context
   window big enough, an allowance not used up, not resting after a failure, and not blocked by a rule.
3. **Score what is left** on five parts, weighted per task class by the policy: *quality* (a measured eval pass rate, or
   a guess from the catalog, moved by feedback), *economy* (price), *headroom* (allowance left), *reliability* (how often
   its calls succeed, learned) and *speed* (learned latency). Rules and similar training examples then add or subtract.

Every guess is labelled as one. Candidates come from `native_agent.router.candidates` or, when that is empty, the free
tool-capable models of the providers configured here plus `native_agent.router.also`; never Claude unless listed.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

TASK_CLASSES = ("trivial", "coding", "hard_reasoning", "long_context", "vision", "bulk")
# Which eval categories say something about which task class.
EVAL_CATEGORIES = {"trivial": ["files"], "coding": ["coding", "files"], "hard_reasoning": ["coding", "search"], "long_context": ["search"],
                   "vision": [], "bulk": ["files"]}
AGENTIC = ("coding", "hard_reasoning", "long_context", "bulk")

_CODE = re.compile(r"(?i)\b(bug|fix|refactor|implement|function|class|method|test|tests|stack ?trace|traceback|exception|compile|lint|patch|diff|commit|"
                   r"repo|repository|endpoint|api|sql|regex|script|module|import|variable)\b|```|def |\bfrom \w+ import\b")
_HARD = re.compile(r"(?i)\b(prove|derive|analy[sz]e|architecture|design|trade-?offs?|strategy|plan|evaluate|compare|why does|root cause|optimi[sz]e|"
                   r"security review|threat model|migrate)\b")
_BULK = re.compile(r"(?i)\b(each of|every (?:row|item|line|file)|for all|classify|categori[sz]e|extract|translate|tag|label|summari[sz]e (?:each|these|the following)|"
                   r"batch|hundreds|thousands)\b")


@dataclass
class Classification:
    task_class: str
    reasons: list[str]
    source: str = "keywords"             # rules (images, size) | keywords | classifier
    scores: dict = field(default_factory=dict)   # the classifier's probabilities, when it was consulted
    keyword_class: str = ""              # what the keywords alone said


@dataclass
class Recommendation:
    model: str
    score: float
    quality: float
    quality_source: str            # "measured" (eval pass rate), "learned" (moved by feedback) or "prior" (a guess from the catalog)
    economy: float
    headroom: Optional[float]
    price_per_mtok: Optional[float]
    context: Optional[int]
    reasons: list[str] = field(default_factory=list)
    reliability: float = 0.75
    speed: float = 0.5
    latency_ms: Optional[float] = None
    calls_ok: float = 0.0
    calls_failed: float = 0.0
    components: dict = field(default_factory=dict)     # each part's value
    contributions: dict = field(default_factory=dict)  # each part's share of the score (value x normalised weight)
    adjustments: list = field(default_factory=list)    # [{"why": ..., "delta": ...}] from rules and training examples
    sampled_score: Optional[float] = None              # the exploration draw, when this pick explored


@dataclass
class Decision:
    id: Optional[int]
    mode: str
    classification: Classification
    ranked: list[Recommendation]
    skipped: list[str]
    chosen: Optional[str]
    explored: bool = False
    greedy: Optional[str] = None
    excluded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("router")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _policy() -> dict:
    from bot.router_brain import policy

    return policy.current()


def _words_match(words: Iterable[str], text: str) -> list[str]:
    low = text.lower()
    return [w for w in words if w and re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low)]


def classify(task: str, *, images: bool = False, context_tokens: int = 0, learned: bool = True) -> Classification:
    text = (task or "").strip()
    if images:
        return Classification("vision", ["the task includes an image"], source="rules", keyword_class="vision")
    if context_tokens >= 60_000 or len(text) > 40_000:
        return Classification("long_context", [f"about {max(context_tokens, len(text) // 4):,} tokens of material"], source="rules",
                              keyword_class="long_context")
    try:
        pol = _policy()
    except Exception:  # noqa: BLE001
        pol = {"keywords": {}, "learning": {"classifier": False}}
    extra = pol.get("keywords") or {}
    hits = {c: _words_match(extra.get(c) or [], text) for c in ("coding", "hard_reasoning", "bulk", "trivial")}
    coding = bool(_CODE.search(text)) or bool(hits["coding"])
    hard = bool(_HARD.search(text)) or bool(hits["hard_reasoning"])
    bulk = bool(_BULK.search(text)) or bool(hits["bulk"])
    custom = [f"your keyword{'s' if len(v) > 1 else ''} {', '.join(repr(w) for w in v[:3])} ({c})" for c, v in hits.items() if v]
    if hits["trivial"] and not coding and not hard:
        base = Classification("trivial", ["matches a keyword you marked trivial"])
    elif bulk and not hard:
        base = Classification("bulk", ["many small independent items"])
    elif hard and (len(text) > 200 or coding):
        base = Classification("hard_reasoning", ["asks for planning, analysis or a trade-off"] + (["about code"] if coding else []))
    elif coding:
        base = Classification("coding", ["mentions code, tests or a repository"])
    elif len(text) <= 160 and not hard:
        base = Classification("trivial", ["a short question with no code or analysis"])
    else:
        base = Classification("hard_reasoning" if hard else "coding" if len(text) > 600 else "trivial",
                              ["a longer request with no clear code signal" if not hard else "asks for analysis"])
    base.reasons += custom
    base.keyword_class = base.task_class
    if not learned or not (pol.get("learning") or {}).get("classifier"):
        return base
    try:
        from bot.router_brain import learn

        view = learn.learned_class(text)
    except Exception:  # noqa: BLE001
        view = None
    if not view:
        return base
    base.scores = view["scores"]
    need = float(pol["learning"].get("classifier_confidence", 0.6))
    if view["task_class"] != base.task_class and view["confidence"] >= need and view["evidence"]:
        return Classification(view["task_class"], [f"your {view['examples']} training example(s) say {view['task_class']} "
                                                   f"({view['confidence']:.0%} sure); the keywords said {base.task_class}"],
                              source="classifier", scores=view["scores"], keyword_class=base.task_class)
    if view["task_class"] == base.task_class:
        base.reasons.append(f"the training examples agree ({view['confidence']:.0%})")
    return base


def _scores_path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir() / "eval_scores.json"


def record_eval_scores(model: str, report: dict) -> dict:
    """Store a live eval report's pass rate per task category for `model` (called by `abp_agenteval run --live --record`)."""
    per: dict[str, list[bool]] = {}
    for t in report.get("results", []):
        per.setdefault(t.get("category", "general"), []).append(bool(t.get("passed")))
    rates = {cat: round(sum(v) / len(v), 3) for cat, v in per.items() if v}
    path = _scores_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[model] = {"rates": rates, "tasks": sum(len(v) for v in per.values()), "mode": report.get("mode", "")}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data[model]


def measured_quality(model: str, task_class: str) -> Optional[float]:
    try:
        data = json.loads(_scores_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(model)
    if not entry or entry.get("mode") != "live":
        return None                                       # a scripted run measures the harness, not a model
    rates = [entry["rates"][c] for c in EVAL_CATEGORIES.get(task_class, []) if c in entry["rates"]]
    return sum(rates) / len(rates) if rates else None


def _prior_quality(info) -> float:
    """A guess in 0..1 from what the catalog says. Deliberately coarse; labelled 'prior' wherever it is shown."""
    q = 0.35
    if info.reasoning:
        q += 0.2
    if info.tool_call:
        q += 0.1
    if info.context:
        q += min(0.1, math.log10(max(info.context, 1000) / 8000) * 0.1)
    if info.price_output:
        q += min(0.25, math.log10(1 + info.price_output) * 0.15)          # price as a rough proxy for capability
    return max(0.0, min(1.0, q))


def _economy(info) -> float:
    if info.free:
        return 1.0
    if info.price_output is None:
        return 0.5
    return max(0.0, 1.0 - math.log10(1 + info.price_output) / 2.0)


def candidate_models() -> list[str]:
    """The models `recommend()` picks from when a caller doesn't name its own list.

    `native_agent.router.candidates`, if set, is used exactly as given — including an
    Anthropic model, if someone explicitly put one there; that's their own choice. The
    *implicit* path (nothing configured, so this falls back to searching the catalog for
    free models) never includes Anthropic: the operator's standing instruction is that
    Claude is never used by default, only when a person explicitly opts into it, and this
    is the one place that guarantee has to hold structurally rather than as an accident of
    which models happen to be marked free in the catalog today."""
    cfg = _cfg()
    listed = [str(m) for m in (cfg.get("candidates") or [])]
    if listed:
        return listed
    from bot import model_catalog
    from bot import providers as registry

    # Only providers configured here can be called, so only their free models are candidates, named the way this
    # install names the provider. (Searching the whole catalog filled the list with models of providers nobody had
    # set up; the router ranked them first and `auto` then failed with every real provider configured.)
    free: list[str] = []
    for name in sorted(registry.list_providers()):
        if model_catalog.catalog_provider_for(name) == "anthropic":
            continue
        for r in model_catalog.search(provider=name, free_only=True, needs=("tools",), limit=12):
            if r["provider"] != "anthropic":
                free.append(f"{name}/{r['model']}")
    return list(dict.fromkeys(free + [str(m) for m in (cfg.get("also") or [])]))


def _when(ts: float) -> str:
    left = ts - time.time()
    if left > 86400:
        return f"for {left / 86400:.1f} more day(s)"
    if left > 3600:
        return f"for {left / 3600:.1f} more hour(s)"
    return f"for {max(1, int(left // 60))} more minute(s)"


def recommend(task: str, *, candidates: Optional[list[str]] = None, images: bool = False, context_tokens: int = 0,
              limit: int = 5, explore: bool = False, cls: Optional[Classification] = None,
              rng: Optional[random.Random] = None) -> tuple[Classification, list[Recommendation], list[str]]:
    """(the classification, ranked recommendations, models left out and why).

    `explore` (automatic routing only) lets a share of picks, set by the policy, re-rank by a random draw from what is
    known about each model, so a promising model that has had few chances gets some; the greedy score stays on each
    recommendation and the draw is kept beside it."""
    from bot import model_catalog
    from bot.agent_runtime import usage_limits
    from bot.router_brain import learn
    from bot.router_brain import policy as policy_mod

    cls = cls or classify(task, images=images, context_tokens=context_tokens)
    know = learn.Knowledge()
    pol = know.policy
    weights = pol["weights"][cls.task_class]
    wsum = sum(weights.values()) or 1.0
    norm = {k: v / wsum for k, v in weights.items()}
    rules = policy_mod.active_rules(pol, cls.task_class)
    try:
        similar = learn.similar_preferences(task) if pol["learning"]["enabled"] else []
    except Exception:  # noqa: BLE001
        similar = []
    out: list[Recommendation] = []
    skipped: list[str] = []
    for ref in candidates if candidates is not None else candidate_models():
        provider, _, model = ref.partition("/")
        blocked = next((r for r in rules if r["kind"] == "block" and policy_mod.rule_matches(r, ref)), None)
        if blocked:
            skipped.append(f"{ref}: blocked by a rule" + (f" ({blocked['note']})" if blocked["note"] else ""))
            continue
        rest = know.cooldown(ref)
        if rest:
            skipped.append(f"{ref}: resting {_when(rest['until'])} after {str(rest['kind'] or 'a failure').replace('_', ' ')}"
                           + (" (set by hand)" if rest["manual"] else f" (strike {rest['strikes']})" if rest["strikes"] > 1 else ""))
            continue
        info = model_catalog.lookup(provider, model)
        if cls.task_class in AGENTIC and info.tool_call is False:
            skipped.append(f"{ref}: no tool calling")
            continue
        if cls.task_class == "vision" and not info.vision:
            skipped.append(f"{ref}: cannot read images")
            continue
        need = max(context_tokens, 0) + 2000
        if info.context and info.context < need:
            skipped.append(f"{ref}: context window {info.context:,} is too small")
            continue
        room = usage_limits.headroom(usage_limits.key_for_provider(provider), model)
        if room is not None and room <= 0:
            skipped.append(f"{ref}: its allowance is used up right now")
            continue
        measured = measured_quality(ref, cls.task_class)
        prior, source = (measured, "measured") if measured is not None else (_prior_quality(info), "prior")
        quality, feedback_n = know.quality(ref, cls.task_class, prior)
        if feedback_n:
            source = "learned" if source == "prior" else source
        rel, okc, failc = know.reliability(ref, cls.task_class)
        spd, lat = know.speed(ref)
        economy = _economy(info)
        h = 1.0 if room is None else room
        comps = {"quality": quality, "economy": economy, "headroom": h, "reliability": rel, "speed": spd}
        contrib = {k: norm[k] * v for k, v in comps.items()}
        adjustments = []
        for r in rules:
            if r["kind"] in ("pin", "prefer", "avoid") and policy_mod.rule_matches(r, ref):
                delta = {"pin": 1.0, "prefer": r["boost"], "avoid": -r["boost"]}[r["kind"]]
                adjustments.append({"why": f"{r['kind']} rule" + (f": {r['note']}" if r["note"] else ""), "delta": round(delta, 3)})
        for s in similar:
            if s["model"] == ref:
                delta = float(pol["learning"]["similar_example_boost"]) * s["similarity"]
                adjustments.append({"why": f"like training example #{s['example_id']} ({s['similarity']:.0%} similar), which prefers it",
                                    "delta": round(delta, 3)})
                break
        score = sum(contrib.values()) + sum(a["delta"] for a in adjustments)
        qlabel = {"measured": "eval pass rate", "learned": f"moved by {feedback_n} piece(s) of feedback", "prior": "a guess from the catalog"}[source]
        reasons = [f"quality {quality:.2f} ({qlabel})",
                   "free" if info.free else (f"${info.price_output:g} per million output tokens" if info.price_output is not None else "price unknown"),
                   "no known limit" if room is None else f"{room:.0%} of its allowance left",
                   f"reliability {rel:.2f}" + (f" ({okc:.0f} ok / {failc:.0f} failed recently)" if okc + failc >= 0.5 else " (untested)")]
        if lat is not None:
            reasons.append(f"about {lat / 1000:.1f}s per call")
        reasons += [f"{a['why']} ({a['delta']:+.2f})" for a in adjustments]
        out.append(Recommendation(ref, round(score, 4), round(quality, 3), source, round(economy, 3), room, info.price_output, info.context,
                                  reasons, round(rel, 3), round(spd, 3), lat, round(okc, 2), round(failc, 2),
                                  {k: round(v, 3) for k, v in comps.items()}, {k: round(v, 4) for k, v in contrib.items()}, adjustments))
    out.sort(key=lambda r: -r.score)
    if explore and out and len(out) > 1 and pol["learning"]["enabled"]:
        rng = rng or random.Random()
        if rng.random() < float(pol["learning"]["explore"]):
            for r in out:
                a, b = r.calls_ok + 3.0, r.calls_failed + 1.0
                k = float(pol["learning"]["prior_strength"]) + 2.0
                q = rng.betavariate(max(0.05, r.quality * k), max(0.05, (1 - r.quality) * k))
                drawn = dict(r.components, reliability=rng.betavariate(a, b), quality=q)
                r.sampled_score = round(sum(norm[c] * v for c, v in drawn.items()) + sum(x["delta"] for x in r.adjustments), 4)
            out.sort(key=lambda r: -(r.sampled_score if r.sampled_score is not None else r.score))
    return cls, out[:max(1, limit)], skipped


def describe(cls: Classification, ranked: list[Recommendation], skipped: list[str]) -> str:
    lines = [f"Task looks like: {cls.task_class} ({'; '.join(cls.reasons)})"]
    if not ranked:
        lines.append("No candidate model fits. " + ("Left out: " + "; ".join(skipped) if skipped else "None are configured (native_agent.router.candidates)."))
        return "\n".join(lines)
    for i, r in enumerate(ranked, 1):
        lines.append(f"{i}. {r.model} - score {r.score:.2f}: " + "; ".join(r.reasons))
    if any(r.quality_source == "prior" for r in ranked):
        lines.append("(Quality figures marked 'a guess' are estimates from the catalog; run the evals for a model to replace them with measurements.)")
    if skipped:
        lines.append("Left out: " + "; ".join(skipped))
    return "\n".join(lines)


# ---- decisions: what automatic routing did, recorded -------------------------------------------------------------------------
def _excerpt(task: str) -> Optional[str]:
    try:
        if not _policy().get("record_task_text", True):
            return None
        from bot.agent_runtime import secrets_guard

        return secrets_guard.redact(str(task or ""))[:240]
    except Exception:  # noqa: BLE001
        return str(task or "")[:240]


def _record(decision: Decision, task: str, instance_id: Optional[int], parent_id: Optional[int], status: str = "pending") -> Optional[int]:
    try:
        from bot.router_brain import policy, store

        chosen_rec = next((r for r in decision.ranked if r.model == decision.chosen), None)
        detail = {"classification": asdict(decision.classification), "candidates": [asdict(r) for r in decision.ranked],
                  "skipped": decision.skipped, "excluded": decision.excluded, "explored": decision.explored, "greedy": decision.greedy,
                  "notes": decision.notes, "weights": policy.current()["weights"].get(decision.classification.task_class),
                  "chosen_score": chosen_rec.score if chosen_rec else None}
        return store.execute(
            "INSERT INTO decisions(ts, mode, instance_id, task_excerpt, task_class, class_source, chosen, explored, policy_version, parent_id, "
            "detail, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision.mode, instance_id, _excerpt(task), decision.classification.task_class, decision.classification.source,
             decision.chosen, 1 if decision.explored else 0, policy.version(), parent_id, json.dumps(detail, default=str), status))
    except Exception:  # noqa: BLE001 — recording is for understanding; it must never stop a turn
        import logging

        logging.getLogger("bot.model_router").exception("router: could not record a decision")
        return None


def route(task: str, *, mode: str = "auto", instance_id: Optional[int] = None, exclude: Iterable[str] = (), images: bool = False,
          context_tokens: int = 0, candidates: Optional[list[str]] = None, usable: Optional[Callable[[str], bool]] = None,
          parent_id: Optional[int] = None, record: bool = True, rng: Optional[random.Random] = None) -> Decision:
    """Pick a model for real and record why. `usable(ref)` may veto a candidate (e.g. its provider is not configured);
    `exclude` holds those already tried this turn."""
    excluded = set(exclude)
    cls, ranked, skipped = recommend(task, candidates=candidates, images=images, context_tokens=context_tokens, limit=50,
                                     explore=mode in ("auto", "failover", "reroute"), rng=rng)
    greedy_order = sorted(ranked, key=lambda r: -r.score)
    notes: list[str] = []
    chosen = None
    for r in ranked:
        if r.model in excluded:
            notes.append(f"{r.model} was already tried this turn")
            continue
        if usable is not None and not usable(r.model):
            notes.append(f"{r.model} is a candidate but its provider is not configured")
            continue
        chosen = r.model
        break
    greedy = next((r.model for r in greedy_order if r.model not in excluded and (usable is None or usable(r.model))), None)
    explored = chosen is not None and greedy is not None and chosen != greedy
    if explored:
        notes.append(f"exploring: tried {chosen} instead of the usual pick {greedy}, to learn how it does")
    d = Decision(None, mode, cls, ranked, skipped, chosen, explored, greedy, sorted(excluded), notes)
    if record:
        d.id = _record(d, task, instance_id, parent_id)
    return d


def record_sticky(ref: str, task: str, *, instance_id: Optional[int], parent_id: Optional[int]) -> Optional[int]:
    """A turn that kept the model an earlier decision chose, recorded so the log shows every turn and its outcome."""
    cls = classify(task)
    d = Decision(None, "sticky", cls, [], [], ref, notes=[f"kept {ref}, chosen in decision #{parent_id}" if parent_id else f"kept {ref}"])
    return _record(d, task, instance_id, parent_id)


def record_advice(task: str, cls: Classification, ranked: list[Recommendation], skipped: list[str], *, instance_id: Optional[int] = None) -> None:
    d = Decision(None, "advise", cls, ranked, skipped, ranked[0].model if ranked else None)
    _record(d, task, instance_id, None, status="advice")


def reroute_reason(ref: str) -> Optional[str]:
    """Why a bot on `auto` should choose again before this turn instead of keeping `ref`, or None to keep it."""
    try:
        from bot.router_brain import learn

        pol = _policy()
        if not pol.get("sticky", True):
            return "choosing again every turn (sticky is off in the policy)"
        rest = learn.cooldown(ref)
        if rest:
            return f"{ref} is resting after {str(rest['kind'] or 'a failure').replace('_', ' ')}"
        blocked = [r for r in pol["rules"] if r["kind"] == "block" and (not r["until"] or r["until"] > time.time())]
        from bot.router_brain import policy as policy_mod

        if any(policy_mod.rule_matches(r, ref) for r in blocked):
            return f"{ref} is blocked by a rule"
    except Exception:  # noqa: BLE001
        return None
    return None


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    async def _suggest(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        cands = inp.get("candidates") if isinstance(inp.get("candidates"), list) else None
        task = str(inp.get("task") or "")
        cls, ranked, skipped = recommend(task, candidates=[str(c) for c in cands] if cands else None,
                                         images=bool(inp.get("images")), context_tokens=int(inp.get("context_tokens") or 0))
        record_advice(task, cls, ranked, skipped, instance_id=instance_id)
        return describe(cls, ranked, skipped)

    toolspec.register(
        {"name": "suggest_model",
         "description": "Advice on which model suits a task (a short question, code work, hard reasoning, long material, images, bulk items), from what "
                        "is known about each candidate's abilities, price, measured eval results, learned reliability and speed, and remaining allowance. "
                        "It only advises: pass the choice to spawn_subagent yourself.",
         "input_schema": {"type": "object", "properties": {"task": {"type": "string"}, "candidates": {"type": "array", "items": {"type": "string"}},
                                                           "images": {"type": "boolean"}, "context_tokens": {"type": "integer"}}, "required": ["task"]}},
        toolspec.ToolSpec("suggest_model", "read", read_only=True, concurrency_safe=True, origin="registered"), _suggest)


register_tools()
