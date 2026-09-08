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
from collections import Counter
from typing import Any, Optional

# Kept well under run_batch()'s 500-char result_excerpt truncation (see
# bot/agent_runtime/subagents.py) — a larger batch per task risks the
# model's real, valid JSON being cut off before it ever reaches us.
EXAMPLES_PER_TASK = 4
DEFAULT_TARGET_INTENT_COUNT = 10


def pick_target_intents(n: int = DEFAULT_TARGET_INTENT_COUNT) -> list[str]:
    """Ranks intents by (fewest existing examples, most recent
    disagreements/unknowns) — the two real signals worth spending a
    swarm dispatch on. See bot/db.py's get_recent_misses()."""
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
    ranked = sorted(all_intents, key=lambda i: (counts.get(i, 0), -miss_counts.get(i, 0)))
    return ranked[:n]


async def _free_provider_models(paid_overrides: dict[str, str]) -> list[tuple[str, str]]:
    """Every configured provider paired with its cheapest (alphabetically
    first) free model id, using the exact same auto-pick order the
    existing native-agent dispatch already uses — plus any provider
    explicitly named in paid_overrides. A provider with neither a known
    free model nor an explicit override is skipped entirely."""
    from bot import providers as providers_mod
    from bot.models import custom_models_with_pricing

    priced, _source = await custom_models_with_pricing()
    selected: list[tuple[str, str]] = []
    for provider_name in sorted(providers_mod.list_providers()):
        entries = priced.get(provider_name, [])
        free_entry = next((e for e in sorted(entries, key=lambda e: e["id"]) if e["free"]), None)
        if free_entry:
            selected.append((provider_name, free_entry["id"]))
        elif provider_name in paid_overrides:
            selected.append((provider_name, paid_overrides[provider_name]))
        # else: no free model and no explicit opt-in for this provider —
        # skipped, never silently downgraded to a paid model.
    return selected


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


async def generate_synthetic_batch(*, parent_instance_id: Optional[int] = None) -> dict[str, Any]:
    """Dispatches the swarm (one child per eligible provider) and stores
    every schema-valid example returned into the pending review queue —
    nothing here ever touches the live training set directly. Returns a
    summary dict for the dashboard's "Generate more training data"
    action."""
    from bot import db
    from bot import swarm_budget
    from bot.agent_runtime import subagents
    from bot.config import config

    tasks = await build_tasks()
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

    pending_added = 0
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
            db.add_support_bot_pending_example(phrase, intent, source_provider=provider, source_model=model)
            pending_added += 1

    return {"dispatched": len(tasks), "pending_added": pending_added, "dispatch_id": result["dispatch_id"]}
