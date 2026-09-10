"""Shared free-model-first provider selection for the Support Bot
subsystem — factored out of synthetic_gen.py's original private
_free_provider_models() (next-generation modular hybrid plan, Phase 6)
so bot/support_bot/llm_fallback.py's Tier 2 picks free models via the
exact same logic synthetic_gen.py's swarm dispatch already used, rather
than two independently-drifting copies of the same free-model-only hard
constraint.
"""

from __future__ import annotations


async def free_provider_models(paid_overrides: dict[str, str]) -> list[tuple[str, str]]:
    """Every configured provider paired with its cheapest (alphabetically
    first) free model id, using the exact same auto-pick order the
    existing native-agent dispatch already uses — plus any provider
    explicitly named in paid_overrides. A provider with neither a known
    free model nor an explicit override is skipped entirely, never
    silently downgraded to paid."""
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
