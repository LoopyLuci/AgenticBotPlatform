"""The three places a rule is actually consulted, and nothing else.

Every function here follows the same three rules, which is what makes "every existing behaviour stays
exactly the same when no rule is set" true rather than aspirational:

1. **No rule, no work.** Each one asks bot.rules.engine.available() and returns "carry on" before it
   touches the database, so an installation with no rules - or with no engine - behaves exactly as it
   did before rules existed.
2. **Fail open, and say so.** A rule that cannot be evaluated (a typo, a limit, a missing engine) is
   logged once with its reason and then treated as "no opinion". This is the same stance
   bot/agent_runtime/hooks.py already takes for a broken hook command and for the same reason: a
   decision-support rule must never be the reason a turn, a routine or a hook stops happening.
3. **Every rule that applies must agree.** Two conditions on one routine both have to say yes; two
   filters on one hook both have to say yes. A rule that says no is a decision, not a silent veto.

    routine_allowed()  does this routine run right now?
    hook_allowed()     does this hook run, given this event and matcher?
    routing()          which backend or model answers this turn?

Each returns a verdict plus the reason, so the caller can log it, put it in a job row, or show it on
a page - and so a test can assert on *why*, not only on what happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from bot.rules import engine, store

logger = logging.getLogger("bot.rules")


@dataclass
class Verdict:
    """What a rule decided, and why - the answer, plus enough to show a person what happened."""

    ok: bool
    reason: str = ""
    rule: str = ""
    answer: object = None            # what a route rule named; None when it named nothing

    def __bool__(self) -> bool:
        return self.ok


ALLOW = Verdict(True)


def _consult(rules: list, context: dict) -> Verdict:
    """Run every rule that applies; the first one that says no is the answer."""
    for rule in rules:
        try:
            if not engine.matches(rule.source, context, rule.kind):
                store.note_result(rule.id)
                return Verdict(False, f"rule {rule.name!r} said no", rule.name)
            store.note_result(rule.id)
        except engine.RuleError as exc:
            store.note_result(rule.id, error=f"{exc} ({exc.where})")
            logger.warning("rule %s could not run (%s at %s); carrying on without it", rule.name, exc, exc.where)
    return ALLOW


# ---- routines ------------------------------------------------------------------------------------
def routine_target(routine_name: str) -> str:
    """What a routine's condition rules are bound to. One place, so the rule and the routine cannot
    disagree about the spelling."""
    return f"routine:{routine_name}"


def routine_allowed(routine: Optional[dict], context: Optional[dict] = None,
                    *, instance_id: Optional[int] = None) -> Verdict:
    """Whether this routine's turn may go ahead. With no engine, or no rule bound to this routine,
    the answer is yes - a routine that was fine before rules existed stays fine."""
    if not routine or not engine.available():
        return ALLOW
    name = str(routine.get("name") or "")
    owner = instance_id if instance_id is not None else routine.get("instance_id")
    if not name or not store.enabled_count("condition", owner):
        return ALLOW
    rules = store.bound(routine_target(name), "condition", owner)
    if not rules:
        return ALLOW
    ctx = context if context is not None else {"routine": name, "instance_id": owner or 0}
    return _consult(rules, ctx)


# ---- hooks ---------------------------------------------------------------------------------------
def hook_targets(event: str, matcher: Optional[str]) -> tuple[str, ...]:
    """The bindings a hook on this event with this matcher is subject to, most specific first: the
    narrowest rule is the one a person wrote for exactly this tool."""
    out = [f"hook:{event}"]
    if matcher:
        out.insert(0, f"hook:{event}:{matcher}")
    out.append("")                        # an unbound filter applies to every hook
    return tuple(out)


def hook_allowed(event: str, matcher: Optional[str], context: Optional[dict] = None, *,
                 instance_id: Optional[int] = None) -> Verdict:
    """Whether the hooks on this event, with this matcher, may run. Every filter bound to one of
    the applicable targets has to say yes."""
    if not engine.available() or not store.enabled_count("filter", instance_id):
        return ALLOW
    ctx = context if context is not None else {"event": event, "matcher": matcher or ""}
    for target in hook_targets(event, matcher):
        rules = store.bound(target, "filter", instance_id)
        if not rules:
            continue
        verdict = _consult(rules, ctx)
        if not verdict.ok:
            return verdict
    return ALLOW


# ---- routing -------------------------------------------------------------------------------------
def routing(context: dict, target: str) -> Verdict:
    """Which backend or model this turn should use, if a rule says so. `target` is "backend" or
    "model"; the first route rule that answers with something usable wins. With no rule bound to
    this target the verdict names no rule, which is how every caller knows to carry on."""
    if target not in store.ROUTE_TARGETS:
        raise ValueError(f"a route targets {store.ROUTE_TARGETS}, not {target!r}")
    if not engine.available():
        return ALLOW
    instance_id = context.get("instance_id") if isinstance(context.get("instance_id"), int) else None
    for rule in store.bound(target, "route", instance_id):
        try:
            answer = engine.evaluate(rule.source, "route", context)
        except engine.RuleError as exc:
            store.note_result(rule.id, error=f"{exc} ({exc.where})")
            logger.warning("route rule %s could not run (%s at %s); routing as usual", rule.name, exc, exc.where)
            continue
        store.note_result(rule.id)
        if answer is None or answer == "" or answer is False:
            continue
        return Verdict(True, f"rule {rule.name!r} chose {answer!r}", rule.name, answer)
    return ALLOW


def routed_backend(context: dict, valid: tuple[str, ...]) -> Optional[str]:
    """The backend a route rule named, or None. A rule that names something that is not a backend is
    logged and ignored rather than handed a name the router cannot route."""
    verdict = routing(context, "backend")
    if not verdict.rule:
        return None
    answer = verdict.answer if isinstance(verdict.answer, str) else ""
    if answer not in valid:
        logger.warning("route rule %s named %r, which is not a backend; routing as usual", verdict.rule, answer)
        return None
    return answer


def routed_model(context: dict) -> Optional[str]:
    """The model a route rule named, or None."""
    verdict = routing(context, "model")
    answer = verdict.answer if isinstance(verdict.answer, str) else ""
    return answer or None