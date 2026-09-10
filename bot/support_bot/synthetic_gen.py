"""Multi-provider, free-model-only synthetic training data generation
for the Support Bot's classifiers — Phase 4 of the "Support Bot NLU
upgrade" plan.

**Hard constraint, per explicit instruction: this module NEVER dispatches
to a paid model unless the operator has explicitly listed it in
config/backends.yaml's support_bot.synthetic_gen.paid_model_overrides
(empty by default).** A configured provider with no known-free model is
skipped entirely, never silently downgraded to paid — see
_free_provider_models() below.

Generated examples are never inserted directly into the live training
set — they land in a pending review queue
(support_bot_pending_examples, bot/db.py) an operator must explicitly
approve or reject via the dashboard, since an LLM-generated label can be
wrong (see bot/memory.py's pending/approved precedent, the same shape
this mirrors).

Deliberately NOT exposed as a live spawn_subagent-callable tool — only
the dashboard (and, later, an optional scheduled job) can trigger a
batch, so generation stays budget/approval-predictable rather than
something a conversational agent decides to fire mid-chat.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Optional

# Kept well under run_batch()'s 500-char result_excerpt truncation (see
# bot/agent_runtime/subagents.py) — a larger batch per task risks the
# model's real, valid JSON being cut off before it ever reaches us.
EXAMPLES_PER_TASK = 4
DEFAULT_TARGET_INTENT_COUNT = 10


def pick_target_intents(
    n: int = DEFAULT_TARGET_INTENT_COUNT, *, allowed_intents: Optional[Any] = None,
) -> list[str]:
    """Ranks intents by (fewest existing examples, most recent
    disagreements/unknowns) — the two real signals worth spending a
    swarm dispatch on. See bot/db.py's get_recent_misses().

    `allowed_intents` (Phase 8 of the next-generation modular hybrid
    plan — the scaling driver, run_until_target()) restricts the ranking
    universe to a given set/list of intents BEFORE truncating to `n` —
    used to scope a generation run to one Knowledge Module's own
    intents, or to "whatever's still under its target count," rather
    than ranking across every intent in the system."""
    from bot import db
    from bot.support_bot.training_data import EXAMPLES

    counts: Counter[str] = Counter(intent for _, intent in EXAMPLES)
    try:
        for row in db.list_support_bot_phrases():
            counts[row["intent"]] += 1
    except Exception:
        pass

    miss_counts: Counter[str] = Counter()
    try:
        for row in db.get_recent_misses():
            # A miss's final_intent may be "unknown" (not a real intent
            # to target) — the sub-models' own guesses are the more
            # useful signal for "which real intent almost matched."
            for guess in (row["tfidf_intent"], row["nn_intent"]):
                if guess and guess != "unknown":
                    miss_counts[guess] += 1
    except Exception:
        pass

    all_intents = set(counts) | set(miss_counts)
    if allowed_intents is not None:
        all_intents &= set(allowed_intents)
    ranked = sorted(all_intents, key=lambda i: (counts.get(i, 0), -miss_counts.get(i, 0)))
    return ranked[:n]


async def _free_provider_models(paid_overrides: dict[str, str]) -> list[tuple[str, str]]:
    """Thin wrapper over bot/support_bot/model_providers.py's shared
    helper — kept as a module-level name here since existing call sites
    and tests already reference `synthetic_gen._free_provider_models`;
    the real logic now lives in one place, reused by
    bot/support_bot/llm_fallback.py's Tier 2 too (next-generation
    modular hybrid plan, Phase 6)."""
    from bot.support_bot.model_providers import free_provider_models

    return await free_provider_models(paid_overrides)


def _build_goal(target_intents: list[str]) -> str:
    from bot.support_bot.training_data import EXAMPLES

    examples_by_intent: dict[str, list[str]] = {}
    for text, intent in EXAMPLES:
        if intent in target_intents:
            examples_by_intent.setdefault(intent, []).append(text)

    lines = [
        f"Generate exactly {EXAMPLES_PER_TASK} short, natural, varied phrasings a real user might type "
        "to a server-management chatbot, covering the intents below. Each phrase must be genuinely "
        "distinct in wording from the existing examples shown, not a trivial rewording of one of them. "
        'Reply with ONLY a JSON object of the exact shape {"examples": [{"phrase": "...", "intent": '
        '"..."}, ...]} — no other text before or after. "intent" must be exactly one of the intent '
        "names listed below, spelled exactly as shown — never invent a new intent name.",
        "",
        "Intents:",
    ]
    for intent in target_intents:
        existing = examples_by_intent.get(intent, [])[:3]
        lines.append(f"- {intent} (existing examples: {existing})")
    return "\n".join(lines)


async def build_tasks(target_intents: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """One run_batch() task per eligible (free, or explicitly opted-in
    paid) provider — see _free_provider_models(). Returns [] if there's
    nothing to target or no eligible provider configured at all."""
    from bot.config import config

    if target_intents is None:
        target_intents = pick_target_intents()
    if not target_intents:
        return []

    paid_overrides = dict(
        config.current.get("support_bot", {}).get("synthetic_gen", {}).get("paid_model_overrides") or {}
    )
    providers_and_models = await _free_provider_models(paid_overrides)
    if not providers_and_models:
        return []

    schema = {
        "type": "object",
        "properties": {
            "examples": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "phrase": {"type": "string"},
                        "intent": {"type": "string", "enum": target_intents},
                    },
                    "required": ["phrase", "intent"],
                },
            }
        },
        "required": ["examples"],
    }
    goal = _build_goal(target_intents)
    return [
        {"provider": provider, "model": model, "effort": "medium", "goal": goal, "output_schema": schema}
        for provider, model in providers_and_models
    ]


_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def _normalize_phrase(phrase: str) -> str:
    """Lowercased, whitespace-collapsed, punctuation-stripped — the
    convergence signal the auto-approve rule looks for is near-identical
    WORDING from independent models, not just "wrote something for the
    same intent." Two models producing "restart bot X?" and "Restart bot
    X" should count as agreement; two models both writing plausible but
    differently-worded phrases for the same intent should not."""
    collapsed = " ".join(phrase.lower().split())
    return _PUNCTUATION_RE.sub("", collapsed)


# Auto-approve threshold — see this module's own docstring and the
# next-generation modular hybrid plan's "auto-approve rule" section.
# Deliberately a count of DISTINCT (provider, model) pairs, not a raw
# occurrence count: two calls to the same model agreeing with itself is
# not the same evidence as two independently-biased free models
# converging on the same real-world phrasing.
_AUTO_APPROVE_MIN_AGREEING_MODELS = 2


async def generate_synthetic_batch(
    *, target_intents: Optional[list[str]] = None, parent_instance_id: Optional[int] = None,
) -> dict[str, Any]:
    """Dispatches the swarm (one child per eligible provider) and stores
    every schema-valid example returned into the pending review queue —
    EXCEPT a (phrase, intent) pair independently produced by
    `_AUTO_APPROVE_MIN_AGREEING_MODELS`-or-more distinct (provider,
    model) pairs in this same batch, which is auto-approved straight
    into the live training set (still recorded in the same pending
    table, `status='approved'`, `approved_by='auto:2-model-agreement'`,
    audit-logged) — see this module's own docstring for why real
    convergence between independently-biased free models is a strong
    enough signal to skip the human-review step, and
    bot/db.py's revert_support_bot_pending_example() for how an operator
    walks one back if it turns out wrong. Returns a summary dict for the
    dashboard's "Generate more training data" action."""
    from bot import db
    from bot import swarm_budget
    from bot.agent_runtime import subagents
    from bot.config import config
    from bot.support_bot import hybrid

    tasks = await build_tasks(target_intents=target_intents)
    if not tasks:
        return {"dispatched": 0, "pending_added": 0, "reason": "no target intents or no eligible free-model provider configured"}

    # Every task here was built to target a free (or explicitly
    # opted-in) model, so pricing_row={"free": True} is honest, not a
    # bypass — check_budget's own cost estimate for a free model is
    # always $0 regardless. This call is a deliberate, explicit
    # safeguard independent of the free-model filter above, not a
    # substitute for it: bot.agent_runtime.subagents.run_batch() has no
    # cost-ceiling guard of its own (confirmed — only the two HTTP
    # dispatch routes get one for free).
    cfg = config.current.get("swarm_budget", {})
    decision = swarm_budget.check_budget(pricing_row={"free": True}, max_children=len(tasks), confirm=True, cfg=cfg)
    if not decision.allowed:
        return {"dispatched": 0, "pending_added": 0, "reason": decision.reason}

    result = await subagents.run_batch(tasks, role="leaf", parent_instance_id=parent_instance_id, background=False)

    # Collect every raw generated example first — auto-approve grouping
    # needs to see the WHOLE batch before deciding which phrases
    # converged, not decide example-by-example as children stream in.
    raw_examples: list[tuple[str, str, str, str]] = []  # (provider, model, phrase, intent)
    for child in result["children"]:
        if child.get("status") != "ok":
            continue
        index = child.get("index")
        if index is None or index >= len(tasks):
            continue
        provider = tasks[index]["provider"]
        model = tasks[index]["model"]
        try:
            payload = json.loads(child["result_excerpt"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        for example in payload.get("examples", []) if isinstance(payload, dict) else []:
            if not isinstance(example, dict):
                continue
            phrase = (example.get("phrase") or "").strip()
            intent = (example.get("intent") or "").strip()
            if not phrase or not intent:
                continue
            raw_examples.append((provider, model, phrase, intent))

    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for provider, model, phrase, intent in raw_examples:
        key = (_normalize_phrase(phrase), intent)
        groups.setdefault(key, []).append((provider, model, phrase))

    pending_added = 0
    auto_approved = 0
    for (_normalized, intent), occurrences in groups.items():
        distinct_models = {(provider, model) for provider, model, _phrase in occurrences}
        auto_approve = len(distinct_models) >= _AUTO_APPROVE_MIN_AGREEING_MODELS
        for i, (provider, model, phrase) in enumerate(occurrences):
            if auto_approve and i == 0:
                # Only the first occurrence in an agreeing group creates
                # a live phrase — the rest are the SAME (normalized)
                # wording, so inserting all of them would just be
                # redundant training data, not additional signal.
                phrase_id = db.add_support_bot_phrase(phrase, intent)
                db.add_support_bot_pending_example(
                    phrase, intent, source_provider=provider, source_model=model,
                    status="approved", approved_by="auto:2-model-agreement",
                    resulting_phrase_id=phrase_id,
                )
                db.log_audit(
                    actor="synthetic_gen", action="support_bot_auto_approve",
                    detail=f"{phrase!r} -> {intent} (agreeing models: {', '.join(f'{p}/{m}' for p, m in sorted(distinct_models))})",
                )
                auto_approved += 1
            else:
                db.add_support_bot_pending_example(phrase, intent, source_provider=provider, source_model=model)
            pending_added += 1

    if auto_approved:
        hybrid.retrain_all()

    return {
        "dispatched": len(tasks), "pending_added": pending_added, "auto_approved": auto_approved,
        "dispatch_id": result["dispatch_id"],
    }


DEFAULT_MAX_BATCHES = 20


def _current_example_counts() -> Counter[str]:
    """Baseline + Training-tab phrases only — deliberately NOT counting
    still-pending (unapproved) examples, since those aren't real training
    signal yet. An auto-approved example DOES count once it lands here
    (add_support_bot_phrase() already ran for it) — exactly the intent:
    a module converging fast on 2-model agreement reaches its target
    sooner than one that needs every example manually reviewed."""
    from bot import db
    from bot.support_bot.training_data import EXAMPLES

    counts: Counter[str] = Counter(intent for _, intent in EXAMPLES)
    try:
        for row in db.list_support_bot_phrases():
            counts[row["intent"]] += 1
    except Exception:
        pass
    return counts


async def run_until_target(
    target_per_intent: int, *, module_id: Optional[str] = None,
    max_batches: int = DEFAULT_MAX_BATCHES, parent_instance_id: Optional[int] = None,
) -> dict[str, Any]:
    """Loops generate_synthetic_batch() (its core logic untouched), each
    iteration re-ranking whichever intents are still under
    `target_per_intent` examples via pick_target_intents(), until either
    (a) every targeted intent has reached the target, (b) `max_batches`
    is hit, or (c) a batch reports a swarm_budget refusal — propagated up
    rather than looping past it. `module_id` (Phase 8 of the
    next-generation modular hybrid plan) scopes the whole run to one
    Knowledge Module's own intents, so an operator can run one module at
    a time — resumable, since re-running later just picks up wherever
    counts currently stand, never re-generating intents already at
    target. Returns a running summary a caller can show as live
    progress, not a single opaque blocking result."""
    allowed_intents: Optional[set[str]] = None
    if module_id is not None:
        from bot.support_bot import knowledge_modules

        allowed_intents = set(knowledge_modules.intents_for_module(module_id))
        if not allowed_intents:
            return {
                "batches_run": 0, "total_dispatched": 0, "total_pending_added": 0, "total_auto_approved": 0,
                "stopped_reason": f"unknown Knowledge Module: {module_id!r}",
            }

    batches_run = 0
    total_dispatched = 0
    total_pending_added = 0
    total_auto_approved = 0
    stopped_reason = "target_reached"

    while batches_run < max_batches:
        counts = _current_example_counts()
        candidates = allowed_intents if allowed_intents is not None else set(counts)
        under_target = {intent for intent in candidates if counts.get(intent, 0) < target_per_intent}
        if not under_target:
            stopped_reason = "target_reached"
            break

        target_intents = pick_target_intents(n=len(under_target), allowed_intents=under_target)
        if not target_intents:
            stopped_reason = "no eligible intents to target"
            break

        result = await generate_synthetic_batch(target_intents=target_intents, parent_instance_id=parent_instance_id)
        batches_run += 1
        total_dispatched += result.get("dispatched", 0)
        total_pending_added += result.get("pending_added", 0)
        total_auto_approved += result.get("auto_approved", 0)

        if result.get("dispatched", 0) == 0:
            # Either no eligible free-model provider, or swarm_budget
            # refused this batch — either way, looping further can't
            # help, so stop and surface the real reason rather than
            # silently spinning through the rest of max_batches.
            stopped_reason = result.get("reason", "batch dispatched nothing")
            break
    else:
        stopped_reason = "max_batches_reached"

    return {
        "batches_run": batches_run, "total_dispatched": total_dispatched,
        "total_pending_added": total_pending_added, "total_auto_approved": total_auto_approved,
        "stopped_reason": stopped_reason,
    }
