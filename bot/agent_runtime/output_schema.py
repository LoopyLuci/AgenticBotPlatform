"""Structured-output contract validation for spawn_subagent tasks —
ported from the real Hermes Agent's own design
(tools/delegation_output_schema.py, confirmed via source): validate a
child's final answer against a caller-supplied JSON Schema, and on
failure send exactly ONE bounded retry turn carrying the literal
validation error — never more. Hermes's own comment on this design is
worth keeping verbatim in spirit: max 1 retry, exact errors, no schema
re-paste beyond what's needed to fix the one mistake.
"""

from __future__ import annotations

import json
from typing import Any

MAX_RETRIES = 1


def _validate(text: str, schema: dict) -> tuple[bool, Any, bool]:
    """Returns (ok, message_or_result, retryable) — `retryable` is False
    when the CALLER's schema itself is malformed (a retry can't fix
    that, only the caller can) so validate_or_retry knows not to waste a
    turn on it."""
    import jsonschema

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"response is not valid JSON: {exc}", True
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return False, str(exc.message), True
    except jsonschema.SchemaError as exc:
        return False, f"output_schema itself is invalid: {exc.message}", False
    return True, text, False


async def validate_or_retry(backend, text: str, schema: dict, *, context: dict, timeout_s: float) -> tuple[bool, str]:
    """Returns (ok, final_text_or_error_summary). `backend` is the same
    NativeAgentBackend the child's first turn ran on — the retry turn
    reuses its session (same context, so /steer-style history continuity
    holds) rather than starting a fresh conversation."""
    ok, result, retryable = _validate(text, schema)
    if ok or not retryable:
        return ok, result

    last_error = result
    for _ in range(MAX_RETRIES):
        retry_prompt = (
            "Your previous answer did not match the required output schema.\n"
            f"Validation error: {last_error}\n\n"
            f"Required JSON Schema: {json.dumps(schema)}\n\n"
            "Reply again with ONLY a single JSON object matching this schema — no other text."
        )
        response = await backend.ask(retry_prompt, context=context, timeout_s=timeout_s)
        ok, result, retryable = _validate(response.text, schema)
        if ok:
            return True, result
        last_error = result
        if not retryable:
            break

    return False, f"output_schema validation failed after {MAX_RETRIES} retry: {last_error}"
