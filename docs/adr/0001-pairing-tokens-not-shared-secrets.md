# ADR-0001: Server-linking uses short-lived pairing tokens, not shared secrets

**Status:** Accepted
**Date:** 2026-08-27

## Context

Server-to-server linking (federation) lets one BotServer install manage
bots running on another machine. The naive approach — copy the target
server's real `DASHBOARD_TOKEN` into the linking server's config — works,
but means a long-lived, full-access credential now exists in two places,
with no expiry and no way to tell it apart from a compromised token used
for anything else that server does.

## Decision

Linking uses a purpose-built, single-use, 10-minute token
(`bsp1.<base64-address>.<secret>`) generated on the target machine and
entered on the linking machine. It is self-describing — the target's own
address is baked into the token via `detect_own_base_url()`'s
UDP-socket-connect trick — so linking needs nothing typed but a name and
the token itself, no IP addresses.

## Consequences

A leaked pairing token is worthless after 10 minutes or one use,
whichever comes first, and its blast radius is "can complete one linking
handshake," not "has full dashboard access forever." The cost is one
extra token-generation step per link (versus copy-pasting an existing
secret) — accepted deliberately, since the whole point is that this
credential should not be reusable.

Alternative considered: OAuth-style device-code flow. Rejected as
over-engineered for a single trust decision made once per pair of
machines, not a recurring login.
