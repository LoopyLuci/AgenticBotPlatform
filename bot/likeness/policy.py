"""The refusal rules for likeness jobs. A refusal is final for that request and is audited like a job.

Checked on every job: the feature is switched on, the consent record exists, is not revoked and attests an adult,
and no text that comes with the job (purpose, names, notes, file names) asks for sexual content or involves a minor.
The text rules are deliberately broad: a false refusal costs a rephrase, a miss is not acceptable."""
from __future__ import annotations

import re
from typing import Iterable, Optional

_SEXUAL = re.compile(
    r"\b(nud(e|es|ity)|naked|nsfw|porn\w*|sex(y|ual|ually)?|erotic\w*|explicit|xxx|lewd|hentai|fetish\w*|strip(ping|per|tease)?"
    r"|topless|bottomless|lingerie|undress\w*|onlyfans|boob\w*|breasts?|nipples?|genital\w*|penis|vagina|orgasm\w*|"
    r"intercourse|masturbat\w*|bdsm|seductive|sensual|horny|kink\w*|deepnude|nudify|cum|blowjob|bikini)\b", re.I)
_MINOR = re.compile(
    r"\b(child(ren)?|kids?|minors?|underage|under[- ]?18|teen(s|age|ager|agers)?|preteen|tween|toddlers?|bab(y|ies)|"
    r"infants?|boys?|girls?|schoolgirl|schoolboy|juvenile|youngster|loli\w*|shota\w*|pupil|kindergarten|"
    r"(1[0-7]|[1-9])[- ]?(yo|y/o|years?[- ]old))\b", re.I)


class Refused(Exception):
    """A likeness request the rules do not allow; the message says why."""


def text_problem(texts: Iterable[Optional[str]]) -> Optional[str]:
    joined = " \n ".join(t for t in texts if isinstance(t, str) and t)
    if not joined:
        return None
    if m := _SEXUAL.search(joined):
        return f"sexual content involving real people is refused (matched {m.group(0)!r})"
    if m := _MINOR.search(joined):
        return f"likeness work involving minors is refused (matched {m.group(0)!r})"
    return None


def check(consent: Optional[dict], texts: Iterable[Optional[str]], *, enabled: bool) -> None:
    """Raise Refused when the job may not run."""
    if not enabled:
        raise Refused("likeness features are switched off (Workspace: Likeness on)")
    if not consent:
        raise Refused("a consent record is required for every real person whose likeness is used")
    if consent.get("revoked"):
        raise Refused(f"consent {consent.get('id')} was revoked")
    if not consent.get("adult"):
        raise Refused("the consent record does not attest that the person is an adult")
    why = text_problem(list(texts) + [consent.get("name"), consent.get("scope")])
    if why:
        raise Refused(why)
