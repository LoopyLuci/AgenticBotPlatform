"""bot/rules - a sandboxed rule language for routines, hooks and routing.

A rule is a decision someone wrote down: *this routine only runs on a weekday*, *only fire this hook
for a tool in the allow list*, *send anything mentioning a screenshot to the vision backend*. Writing
one of those as Python or as a shell command would mean either giving a rule the agent's own rights
or giving up the thing that makes a rule useful, which is being able to say it in one line.

So a rule is a small expression, in a language with no filesystem, no network, no clock and no
processes, and with hard limits on everything it can consume (native/abp-rules, `abp_rules`). It is
evaluated against a map of plain values - the message, who sent it, which bot, what time it is, some
tags - and it answers either yes or no, or names one thing to use instead of another.

    from bot import rules

    rules.validate('ctx.weekday < 5 && ctx.hour >= 9', "condition")
    rules.matches('ctx.weekday < 5 && ctx.hour >= 9', rules.context.from_message("standup please"))

The four modules:

    engine       the abp_rules bridge, and what happens when it is not installed
    store        the rules themselves, in one small table beside routines and hooks
    context      the only things a rule can see
    integration  the three places a rule is consulted: routines, hook filters, routing

Nothing here changes behaviour on its own. Every integration point asks whether a rule is bound to
it and, when none is, carries on exactly as before - and when there is no engine installed, rules are
off with a clear message and ABP is otherwise untouched.
"""
from __future__ import annotations

from bot.rules import context, engine, integration, store
from bot.rules.context import build as build_context
from bot.rules.context import from_event as event_context
from bot.rules.context import from_message as message_context
from bot.rules.engine import (
    KINDS,
    DISABLED_MESSAGE,
    RuleError,
    RulesDisabled,
    available,
    backend,
    evaluate,
    is_enabled,
    limits,
    matches,
    status,
    test,
    validate,
)
from bot.rules.integration import hook_allowed, routine_allowed, routed_backend, routed_model, routing
from bot.rules.store import Rule, RuleStoreError

__all__ = [
    "DISABLED_MESSAGE", "KINDS", "Rule", "RuleError", "RuleStoreError", "RulesDisabled",
    "available", "backend", "build_context", "context", "engine", "evaluate", "hook_allowed",
    "integration", "is_enabled", "limits", "matches", "message_context", "event_context",
    "routine_allowed", "routed_backend", "routed_model", "routing", "status", "store", "test", "validate",
]