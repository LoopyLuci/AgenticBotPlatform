"""An advisory model router (roadmap P8): which of your models suits this task, and why.

    recommend("fix the failing test in auth.py", candidates=[...]) -> [Recommendation(model, score, reasons, ...), ...]

It **advises; it never switches anything by itself.** The agent has a `suggest_model` tool, `/route <task>` shows the ranking
in chat, and a person or a delegating agent decides. There is no learned model here - a plain, inspectable set of rules:

1. **Classify the task** from its text and what it needs: `trivial` (a short question, a greeting), `coding`, `hard_reasoning`
   (planning, analysis, trade-offs, proofs), `long_context` (a lot of material), `vision` (images), `bulk` (many small
   independent items: classify, extract, translate, summarise a list).
2. **Filter the candidates** to those that can do it: tool calling for anything agentic, image input for vision, a context
   window big enough, and an allowance that is not used up right now (usage_limits.py).
3. **Score what is left** from what is known: a *quality* estimate, an *economy* estimate and *headroom*, weighted by task class
   (quality counts most for hard tasks; cost and free allowance count most for trivial and bulk ones).

The quality estimate is the model's **measured pass rate on the eval suite for that kind of task when one has been recorded**
(`python -m abp_agenteval run --live --record`), and otherwise a *prior* from the catalog: reasoning support, context size,
recency, and price as a rough proxy for capability. The prior is a guess and is labelled as one in each recommendation; the
router says so instead of pretending to know. Run the evals for the models you care about and it stops guessing.

Candidates come from `native_agent.router.candidates` (a list of "provider/model") or, if that is empty, from the models ABP
already knows of that are free plus any you list under `native_agent.router.also`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TASK_CLASSES = ("trivial", "coding", "hard_reasoning", "long_context", "vision", "bulk")
# Which eval categories say something about which task class.
EVAL_CATEGORIES = {"trivial": ["files"], "coding": ["coding", "files"], "hard_reasoning": ["coding", "search"], "long_context": ["search"],
                   "vision": [], "bulk": ["files"]}
# (quality, economy, headroom) weights per class.
WEIGHTS = {"trivial": (0.15, 0.55, 0.30), "bulk": (0.20, 0.45, 0.35), "coding": (0.55, 0.15, 0.30), "long_context": (0.50, 0.15, 0.35),
           "hard_reasoning": (0.70, 0.05, 0.25), "vision": (0.50, 0.15, 0.35)}
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


@dataclass
class Recommendation:
    model: str
    score: float
    quality: float
    quality_source: str            # "measured" (eval pass rate) or "prior" (a guess from the catalog)
    economy: float
    headroom: Optional[float]
    price_per_mtok: Optional[float]
    context: Optional[int]
    reasons: list[str] = field(default_factory=list)


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("router")) or {}
    except Exception:  # noqa: BLE001
        return {}


def classify(task: str, *, images: bool = False, context_tokens: int = 0) -> Classification:
    text = (task or "").strip()
    if images:
        return Classification("vision", ["the task includes an image"])
    if context_tokens >= 60_000 or len(text) > 40_000:
        return Classification("long_context", [f"about {max(context_tokens, len(text) // 4):,} tokens of material"])
    coding, hard, bulk = bool(_CODE.search(text)), bool(_HARD.search(text)), bool(_BULK.search(text))
    if bulk and not hard:
        return Classification("bulk", ["many small independent items"])
    if hard and (len(text) > 200 or coding):
        return Classification("hard_reasoning", ["asks for planning, analysis or a trade-off"] + (["about code"] if coding else []))
    if coding:
        return Classification("coding", ["mentions code, tests or a repository"])
    if len(text) <= 160 and not hard:
        return Classification("trivial", ["a short question with no code or analysis"])
    return Classification("hard_reasoning" if hard else "coding" if len(text) > 600 else "trivial",
                          ["a longer request with no clear code signal" if not hard else "asks for analysis"])


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

    free = [f"{r['provider']}/{r['model']}" for r in model_catalog.search(free_only=True, needs=("tools",), limit=12)
            if r["provider"] != "anthropic"]
    return free + [str(m) for m in (cfg.get("also") or [])]


def recommend(task: str, *, candidates: Optional[list[str]] = None, images: bool = False, context_tokens: int = 0,
              limit: int = 5) -> tuple[Classification, list[Recommendation], list[str]]:
    """(the classification, ranked recommendations, models left out and why)."""
    from bot import model_catalog
    from bot.agent_runtime import usage_limits

    cls = classify(task, images=images, context_tokens=context_tokens)
    wq, we, wh = WEIGHTS[cls.task_class]
    out: list[Recommendation] = []
    skipped: list[str] = []
    for ref in candidates if candidates is not None else candidate_models():
        provider, _, model = ref.partition("/")
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
        quality, source = (measured, "measured") if measured is not None else (_prior_quality(info), "prior")
        economy = _economy(info)
        h = 1.0 if room is None else room
        reasons = [f"quality {quality:.2f} ({'eval pass rate' if measured is not None else 'a guess from the catalog'})",
                   "free" if info.free else (f"${info.price_output:g} per million output tokens" if info.price_output is not None else "price unknown"),
                   "no known limit" if room is None else f"{room:.0%} of its allowance left"]
        out.append(Recommendation(ref, round(wq * quality + we * economy + wh * h, 4), round(quality, 3), source, round(economy, 3),
                                  room, info.price_output, info.context, reasons))
    out.sort(key=lambda r: -r.score)
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


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    async def _suggest(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        cands = inp.get("candidates") if isinstance(inp.get("candidates"), list) else None
        cls, ranked, skipped = recommend(str(inp.get("task") or ""), candidates=[str(c) for c in cands] if cands else None,
                                         images=bool(inp.get("images")), context_tokens=int(inp.get("context_tokens") or 0))
        return describe(cls, ranked, skipped)

    toolspec.register(
        {"name": "suggest_model",
         "description": "Advice on which model suits a task (a short question, code work, hard reasoning, long material, images, bulk items), from what "
                        "is known about each candidate's abilities, price, measured eval results and remaining allowance. It only advises: pass the "
                        "choice to spawn_subagent yourself.",
         "input_schema": {"type": "object", "properties": {"task": {"type": "string"}, "candidates": {"type": "array", "items": {"type": "string"}},
                                                           "images": {"type": "boolean"}, "context_tokens": {"type": "integer"}}, "required": ["task"]}},
        toolspec.ToolSpec("suggest_model", "read", read_only=True, concurrency_safe=True, origin="registered"), _suggest)


register_tools()
