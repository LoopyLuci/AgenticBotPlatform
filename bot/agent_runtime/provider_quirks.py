"""Per-provider wire-protocol quirks for the generic OpenAI-compatible
transport — confirmed against Hermes Agent's real per-vendor adapter
plugins (`plugins/model-providers/<vendor>/__init__.py` in its installed
source), not guessed. A single generic chat-completions client silently
misbehaves or hard-fails on several real, commonly-used providers:

- **DeepSeek** (V4 family): 400s on a SECOND tool call in the same
  conversation unless `extra_body.thinking` is explicitly present on
  every request (a real, documented API bug worked around by always
  sending the field, not just when thinking is wanted).
- **Kimi/Moonshot**: 400s if both `extra_body.thinking` AND a top-level
  `reasoning_effort` are sent together — the two are mutually exclusive
  on this wire.
- **Z.AI / GLM**: needs `extra_body.thinking: {"type": "enabled"|"disabled"}`
  or it silently keeps burning thinking tokens regardless of the
  requested effort; GLM-5.2 only accepts `high`/`max` on its own
  `reasoning_effort` field, GLM-5.3 accepts the full low..max range.
- **OpenRouter**: some models 4xx on an effort level they don't
  advertise support for in their own catalog — this module clamps down
  to a safe default set rather than trying to look up
  model-by-model live capability (a real per-model catalog lookup is a
  separate, larger feature; clamping to the safe, universally-accepted
  {low, medium, high} set here avoids the failure mode without it).
- **Ollama Cloud**: has a real, undocumented `max` reasoning tier one
  step above `xhigh` that the generic 3-tier passthrough would collapse
  away.
- **OpenCode Zen/Go**: requires an `x-opencode-session` header (a stable
  per-conversation id — OpenCode's own usage/cost-caching page keys off
  it) on every request, or free-tier models reject the call outright
  with a `MissingSessionID` error. Confirmed against OpenCode's own
  issue tracker (earendil-works/pi#4847), which documents this exact
  header plus an optional `x-opencode-client` self-identifying label
  used only for their own usage stats — NOT a value that needs to claim
  to be their own CLI; any honest client name is fine and is the
  documented, sanctioned way third-party tools (pi, OpenClaw, etc.) call
  this API.

Matching is done by keyword against the provider's `catalog_id` (from
config/providers.yaml) AND its base_url, not an exact string — real
catalog identifiers can vary in spelling/casing, and this only needs to
be right often enough to avoid the specific documented failure modes
above, not exhaustively exact.
"""

from __future__ import annotations

from typing import Any, Optional


class QuirkProfile:
    def __init__(
        self,
        *,
        force_thinking_extra_body: bool = False,
        mutually_exclusive_thinking_and_effort: bool = False,
        clamp_effort_to: Optional[set] = None,
        effort_field_override: Optional[str] = None,
        needs_opencode_session_header: bool = False,
    ):
        self.force_thinking_extra_body = force_thinking_extra_body
        self.mutually_exclusive_thinking_and_effort = mutually_exclusive_thinking_and_effort
        self.clamp_effort_to = clamp_effort_to
        self.effort_field_override = effort_field_override
        self.needs_opencode_session_header = needs_opencode_session_header


_PROFILES: dict[str, QuirkProfile] = {
    "deepseek": QuirkProfile(force_thinking_extra_body=True),
    "kimi": QuirkProfile(mutually_exclusive_thinking_and_effort=True),
    "moonshot": QuirkProfile(mutually_exclusive_thinking_and_effort=True),
    "zai": QuirkProfile(force_thinking_extra_body=True),
    "z-ai": QuirkProfile(force_thinking_extra_body=True),
    "glm": QuirkProfile(force_thinking_extra_body=True),
    "openrouter": QuirkProfile(clamp_effort_to={"low", "medium", "high"}),
    "ollama": QuirkProfile(),  # no clamp needed — this transport already treats "max" as a valid passthrough value
    "opencode": QuirkProfile(needs_opencode_session_header=True),
}


def profile_for(catalog_id: Optional[str], base_url: str) -> Optional[QuirkProfile]:
    """The quirk profile to apply for a resolved provider, or None (every
    override below is then simply skipped, leaving today's generic
    behavior exactly as it was) if nothing matches."""
    haystack = f"{(catalog_id or '').lower()} {base_url.lower()}"
    for keyword, profile in _PROFILES.items():
        if keyword in haystack:
            return profile
    return None


# Self-identifying client label sent via the (optional, per OpenCode's
# own docs) x-opencode-client header — used only for OpenCode's own
# per-tool usage stats, same purpose as any provider's User-Agent
# convention. Always this project's own honest name, never a value
# claiming to be OpenCode's own CLI.
OPENCODE_CLIENT_LABEL = "botserver"


def extra_headers(*, profile: Optional[QuirkProfile], session_id: Optional[str]) -> dict[str, str]:
    """Additional HTTP headers a provider's quirk profile requires,
    beyond the transport's own Content-Type/Authorization — currently
    just OpenCode Zen/Go's required session header. A provider with no
    profile (or no header-related quirk) gets an empty dict, changing
    nothing about today's request shape."""
    if profile is None or not profile.needs_opencode_session_header:
        return {}
    headers = {"x-opencode-client": OPENCODE_CLIENT_LABEL}
    if session_id:
        headers["x-opencode-session"] = session_id
    return headers


def apply(payload: dict[str, Any], *, profile: Optional[QuirkProfile], effort: Optional[str]) -> None:
    """Mutates `payload` (already built by the generic transport) in
    place, applying only the overrides `profile` actually declares. A
    provider with no profile is untouched — purely additive."""
    if profile is None:
        return
    thinking_wanted = effort not in (None, "none")
    if profile.force_thinking_extra_body:
        extra_body = payload.setdefault("extra_body", {})
        extra_body["thinking"] = {"type": "enabled" if thinking_wanted else "disabled"}
    if profile.mutually_exclusive_thinking_and_effort and "extra_body" in payload and "thinking" in payload.get("extra_body", {}):
        # Prefer the explicit thinking toggle over a top-level effort
        # field when this provider rejects sending both — confirmed
        # against Kimi's real documented behavior.
        payload.pop("reasoning_effort", None)
    if profile.clamp_effort_to is not None and payload.get("reasoning_effort") not in (None, *profile.clamp_effort_to):
        payload["reasoning_effort"] = "high" if "high" in profile.clamp_effort_to else next(iter(profile.clamp_effort_to))
