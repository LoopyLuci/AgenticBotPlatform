"""Generative UI: describe a change in plain English, get a real,
working modification of BotServer's own dashboard/desktop-app HTML+JS,
review a live preview and diff, and only touch the real file on
explicit approval — with an instant, exact revert if anything's wrong.

Deliberately scoped to the frontend UI files only (see TARGETS below),
never backend Python. bot/hotreload.py's own DENYLIST (router.py, db.py,
bot/dashboard/server.py, main.py) exists because those hold live DB
connections, in-flight WebSocket sessions, and the FastAPI app object
itself — none of which can be safely hot-swapped. Building an LLM-driven
"never crashes" patcher for those files would contradict a safety
boundary this project already chose deliberately elsewhere.

"Without crashing" is satisfied honestly as: never written to the real
file without a human reviewing a live preview + diff first, always
backed up before writing, always one-click revertable — NOT as "the
generated code is guaranteed bug-free," which no system can promise.

Model output format is deliberately marker-delimited plain text, not
JSON: stuffing a multi-thousand-line HTML file into a JSON string value
is fragile (one escaping mistake corrupts the whole parse). A fenced
code block parsed by plain string splitting is far more robust for a
payload this large than bot/agent_runtime/output_schema.py's
JSON-Schema validation, which fits small objects, not whole files.
"""

from __future__ import annotations

import difflib
import html.parser
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from bot.envfile import PROJECT_ROOT

TARGETS = {
    "dashboard": PROJECT_ROOT / "bot" / "dashboard" / "static" / "dashboard.html",
    "desktop_html": PROJECT_ROOT / "desktop-app" / "ui" / "index.html",
    "desktop_js": PROJECT_ROOT / "desktop-app" / "ui" / "main.js",
}

HISTORY_ROOT = PROJECT_ROOT / "data" / "ui_customize_history"
PENDING_ROOT = HISTORY_ROOT / "pending"
BACKUPS_ROOT = HISTORY_ROOT / "backups"
MANIFEST_PATH = HISTORY_ROOT / "manifest.json"

# Load-bearing for auth and for this feature's own live-refresh broadcast —
# a generated dashboard.html that loses these has broken itself, not just
# removed a cosmetic element. Checked only for target == "dashboard".
_CRITICAL_DASHBOARD_STRINGS = ("getToken", "connectLiveEventsSocket")

_MIN_LENGTH_FRACTION = 0.3


class UiCustomizeError(Exception):
    pass


# --------------------------------------------------------------- targets --

def _target_path(target: str) -> Path:
    path = TARGETS.get(target)
    if path is None:
        raise UiCustomizeError(f"unknown target {target!r} — must be one of {sorted(TARGETS)}")
    return path


def _is_html_target(target: str) -> bool:
    return target in ("dashboard", "desktop_html")


# ------------------------------------------------------------- generation --

_PROMPT_TEMPLATE = """You are editing one real, live file inside a running application called BotServer.
File: {filename}
This file is the ENTIRE current content — it is a single self-contained HTML document with inline <style> and <script> blocks (or, for a .js file, plain JavaScript). Do not assume any other file exists alongside it for style/script — everything is inline.

The user's requested change:
{instruction}

Requirements:
- Make ONLY the change requested, preserving every other part of the file exactly as it is — same structure, same existing IDs, same existing functionality, same coding style/conventions already used in the file (e.g. how buttons/sections/JS functions are already written).
- Never remove or rename an existing element id unless the user's instruction specifically asks to remove that element.
- Output real, syntactically valid, working code — this will be checked (HTML tag balance, JS syntax) before it's ever applied.

Reply in EXACTLY this format, nothing else before or after:
EXPLANATION: <one or two sentences describing what you changed>
```{lang}
<the COMPLETE new file content, from the very first character to the very last>
```
"""


# This project's model-pricing data (bot/model_pricing.py /
# custom_models_with_pricing()) doesn't track context-window size — only
# free/input-cost/output-cost. Regenerating a whole multi-thousand-line
# file needs a model whose context window can hold the ENTIRE file twice
# over (once as input, once as the generated output) — a small "mini"
# model can silently truncate mid-file with no error, just a malformed
# response _extract_generation() then correctly refuses to guess at.
# Absent real context-length metadata, this is a plain name-substring
# heuristic for free-tier models commonly shipped with large (100k+)
# context windows — checked in order, first match wins; anything not
# matched falls through to the plain alphabetical-first pick as before.
_LARGE_CONTEXT_FREE_MODEL_HINTS = (
    "gemini", "deepseek", "llama-3.1", "llama-3.3", "qwen", "grok", "glm-4.5", "kimi",
)


async def _candidate_free_models() -> list[tuple[Optional[str], str]]:
    """Every free model from any configured provider, ranked with
    likely-large-context ones first (see _LARGE_CONTEXT_FREE_MODEL_HINTS)
    then the rest alphabetically — a ranked list, not a single pick, so
    _generate_raw() can retry against the next candidate if one turns
    out too small for the file being edited. Falls back to a real
    Anthropic model if no free custom model is configured anywhere."""
    from bot import providers as providers_mod
    from bot.models import custom_models_with_pricing

    try:
        priced, _source = await custom_models_with_pricing()
    except Exception:
        priced = {}

    all_free: list[tuple[Optional[str], str]] = []
    for provider_name in sorted(providers_mod.list_providers()):
        entries = priced.get(provider_name, [])
        for entry in sorted(entries, key=lambda e: e["id"]):
            if entry.get("free"):
                all_free.append((provider_name, entry["id"]))

    if not all_free:
        return [(None, "claude-sonnet-5")]

    def _rank(candidate: tuple[Optional[str], str]) -> tuple[int, str]:
        _, model_id = candidate
        lowered = model_id.lower()
        for i, hint in enumerate(_LARGE_CONTEXT_FREE_MODEL_HINTS):
            if hint in lowered:
                return (i, model_id)
        return (len(_LARGE_CONTEXT_FREE_MODEL_HINTS), model_id)

    return sorted(all_free, key=_rank)


class _MalformedGenerationError(UiCustomizeError):
    """Raised by _extract_generation() specifically — distinguished from
    a general UiCustomizeError so _generate_raw() can catch exactly this
    (a parse failure, most often caused by the model truncating a large
    file mid-output) and retry against a different candidate model,
    without also swallowing genuinely unrelated errors."""


def _extract_generation(raw: str) -> tuple[str, str]:
    """Parses the marker-delimited "EXPLANATION: ...\\n```lang\\n...\\n```"
    format. Raises _MalformedGenerationError with a clear reason on
    anything else — never silently guesses at a malformed response."""
    m = re.search(r"EXPLANATION:\s*(.*?)\n```[a-zA-Z]*\n(.*)\n```\s*$", raw, re.S)
    if not m:
        raise _MalformedGenerationError(
            "the model's reply didn't match the expected EXPLANATION + fenced-code-block format — most "
            "often this means the model ran out of output space partway through a large file, rather than "
            "genuinely misunderstanding the request"
        )
    explanation = m.group(1).strip()
    new_content = m.group(2)
    return explanation, new_content


_MAX_MODEL_ATTEMPTS = 3


async def _generate_raw(target: str, instruction: str, current_content: str) -> tuple[str, str]:
    """Returns (explanation, new_content). Tries up to _MAX_MODEL_ATTEMPTS
    candidate free models in rank order (see _candidate_free_models()),
    moving to the next one whenever a candidate's response doesn't parse
    — this is exactly the failure mode a too-small-context model produces
    on a large file, so retrying against a different model is the right
    response, not just failing outright on the first miss."""
    # _single_call is moa.py's internal single-shot completion helper (no
    # tool loop needed for a read+generate task) — reused directly rather
    # than going through consult()'s multi-reference wrapper, since this
    # only ever needs exactly one model call per attempt.
    from bot.agent_runtime.moa import _single_call

    filename = _target_path(target).name
    lang = "html" if _is_html_target(target) else "javascript"
    prompt = _PROMPT_TEMPLATE.format(filename=filename, instruction=instruction, lang=lang)
    prompt += f"\n\nCurrent file content:\n```{lang}\n{current_content}\n```\n"

    candidates = (await _candidate_free_models())[:_MAX_MODEL_ATTEMPTS]
    last_error: Optional[Exception] = None
    for provider, model in candidates:
        # Generating a whole file needs far more headroom than moa.py's
        # own MAX_TOKENS=4096 (tuned for short consult() answers) — a
        # several-thousand-line HTML file can easily need tens of
        # thousands of output tokens.
        raw = await _single_call(provider, model, prompt, max_tokens=32000, timeout_s=300.0)
        try:
            return _extract_generation(raw)
        except _MalformedGenerationError as exc:
            last_error = exc
            continue
    raise UiCustomizeError(
        f"tried {len(candidates)} free model(s) and none returned a complete response for this file — "
        "it's likely too large for any currently-configured free model's context window; try a smaller, "
        "more targeted instruction, or configure a provider with a larger-context free tier"
    ) from last_error


# ------------------------------------------------------------- validation --

class _BalanceHTMLParser(html.parser.HTMLParser):
    """Tracks open/close tag balance across the whole document. Void
    elements (br, img, input, ...) never need a matching close tag, so
    they're excluded from the balance count rather than causing a false
    mismatch."""

    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.max_negative = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag not in self._VOID:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        pass  # self-closed (<tag ... />) — never opens anything to balance

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._VOID:
            self.depth -= 1
            self.max_negative = min(self.max_negative, self.depth)


def _check_html_balance(content: str) -> list[str]:
    parser = _BalanceHTMLParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:  # pragma: no cover - HTMLParser is very lenient, rarely raises
        return [f"HTML failed to parse: {exc}"]
    errors = []
    if parser.max_negative < 0:
        errors.append("a closing tag appears with no matching open tag somewhere in the document")
    if parser.depth != 0:
        errors.append(f"{parser.depth} tag(s) are opened but never closed (or vice versa)")
    return errors


def _extract_script_blocks(content: str) -> list[str]:
    blocks = []
    for m in re.finditer(r"<script(\s[^>]*)?>(.*?)</script>", content, re.S | re.I):
        attrs = m.group(1) or ""
        if "src=" in attrs:
            continue  # external script — nothing inline to check
        body = m.group(2)
        if body.strip():
            blocks.append(body)
    return blocks


def _extract_style_blocks(content: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<style(?:\s[^>]*)?>(.*?)</style>", content, re.S | re.I)]


def _check_js_syntax(js_source: str) -> tuple[bool, Optional[str]]:
    """Returns (checked, error). `checked=False` means node wasn't
    available and no real check ran at all — the caller must fall back
    to _check_js_balance in that case. `checked=True, error=None` means
    node ran and found no problem. Uses `node --check`, already part of
    this repo's own toolchain via the Tauri build."""
    import shutil

    node = shutil.which("node")
    if not node:
        return False, None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js_source)
        path = f.name
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return True, result.stderr.strip()[:500]
        return True, None
    except Exception as exc:  # pragma: no cover
        return True, f"couldn't run node --check: {exc}"
    finally:
        Path(path).unlink(missing_ok=True)


def _check_js_balance(js_source: str) -> Optional[str]:
    """Zero-dependency fallback when node isn't on PATH — documented,
    not silent: weaker than a real parser (doesn't understand strings/
    regex literals containing brackets), but catches gross truncation."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    for ch in js_source:
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in pairs.values():
            if not stack or stack.pop() != ch:
                return "unbalanced brackets (fallback check — node was not available for a real syntax check)"
    if stack:
        return "unbalanced brackets (fallback check — node was not available for a real syntax check)"
    return None


def _extract_ids(content: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', content))


def _count_sections(content: str) -> int:
    return len(re.findall(r"<section\b", content, re.I))


def validate_change(target: str, original_content: str, new_content: str) -> dict:
    """Returns {"errors": [...], "warnings": [...], "removed_ids": [...]}.
    Non-empty `errors` means apply_change() must refuse. `warnings`/
    `removed_ids` are surfaced to the human reviewer but never block
    apply on their own — a user might legitimately ask to remove
    something."""
    errors: list[str] = []
    warnings: list[str] = []

    if len(new_content) < len(original_content) * _MIN_LENGTH_FRACTION:
        errors.append(
            f"generated content is only {len(new_content)} chars vs. the original's "
            f"{len(original_content)} — looks truncated, refusing before running further checks"
        )
        return {"errors": errors, "warnings": warnings, "removed_ids": []}

    is_html = _is_html_target(target)

    if is_html:
        errors.extend(_check_html_balance(new_content))
        for block in _extract_script_blocks(new_content):
            checked, js_error = _check_js_syntax(block)
            if checked and js_error:
                errors.append(f"inline <script> failed to validate: {js_error}")
            elif not checked:
                fallback = _check_js_balance(block)
                if fallback:
                    warnings.append(fallback)
        for block in _extract_style_blocks(new_content):
            if block.count("{") != block.count("}"):
                errors.append("an inline <style> block has unbalanced braces")
    else:
        checked, js_error = _check_js_syntax(new_content)
        if checked and js_error:
            errors.append(f"file failed to validate: {js_error}")
        elif not checked:
            fallback = _check_js_balance(new_content)
            if fallback:
                warnings.append(fallback)

    removed_ids: list[str] = []
    if is_html:
        before_ids = _extract_ids(original_content)
        after_ids = _extract_ids(new_content)
        removed_ids = sorted(before_ids - after_ids)

        if target == "dashboard":
            for needle in _CRITICAL_DASHBOARD_STRINGS:
                if needle in original_content and needle not in new_content:
                    errors.append(
                        f"critical structural anchor {needle!r} is present in the current file but "
                        "missing from the generated one — this backs authentication or this feature's "
                        "own live-refresh mechanism, refusing to apply"
                    )
            before_sections = _count_sections(original_content)
            after_sections = _count_sections(new_content)
            if after_sections < before_sections:
                errors.append(
                    f"the number of <section> blocks dropped from {before_sections} to {after_sections} "
                    "— refusing to apply a change that removes whole sections of the dashboard unexpectedly"
                )

    return {"errors": errors, "warnings": warnings, "removed_ids": removed_ids}


# ---------------------------------------------------------- generate/apply --

def _ensure_dirs() -> None:
    PENDING_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)


def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_manifest(entries: list[dict]) -> None:
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


async def generate_change(target: str, instruction: str) -> dict:
    """Reads the real current file, asks a model to produce the full new
    content, validates it, computes a diff, and caches the result
    server-side under a fresh change_id — never writes to the real
    target file. Returns the dict the dashboard's preview panel needs."""
    if not instruction or not instruction.strip():
        raise UiCustomizeError("instruction must not be empty")
    path = _target_path(target)
    if not path.exists():
        raise UiCustomizeError(f"target file does not exist: {path}")
    original_content = path.read_text(encoding="utf-8")

    explanation, new_content = await _generate_raw(target, instruction, original_content)
    result = validate_change(target, original_content, new_content)

    diff_lines = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"{target} (current)",
            tofile=f"{target} (proposed)",
        )
    )
    diff_text = "".join(diff_lines)

    change_id = uuid.uuid4().hex[:16]
    _ensure_dirs()
    pending_record = {
        "change_id": change_id,
        "target": target,
        "instruction": instruction,
        "explanation": explanation,
        "new_content": new_content,
        "errors": result["errors"],
        "warnings": result["warnings"],
        "removed_ids": result["removed_ids"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (PENDING_ROOT / f"{change_id}.json").write_text(json.dumps(pending_record), encoding="utf-8")

    return {
        "change_id": change_id,
        "target": target,
        "explanation": explanation,
        "diff": diff_text,
        "preview_html": new_content if _is_html_target(target) else None,
        "valid": not result["errors"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "removed_ids": result["removed_ids"],
    }


def apply_change(change_id: str, *, actor: str = "dashboard") -> dict:
    """Looks up the cached pending result BY change_id only — never
    accepts raw content from a caller. This means the exact bytes shown
    in the diff/preview are the exact bytes written; a client can't
    (accidentally or otherwise) apply something different from what was
    reviewed. Re-validates before writing since the pending record could
    theoretically be stale (the real file changed since generation)."""
    pending_path = PENDING_ROOT / f"{change_id}.json"
    if not pending_path.exists():
        raise UiCustomizeError(f"no pending change {change_id!r} — it may have already been applied or discarded")
    record = json.loads(pending_path.read_text(encoding="utf-8"))

    target = record["target"]
    path = _target_path(target)
    original_content = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = record["new_content"]

    revalidated = validate_change(target, original_content, new_content)
    if revalidated["errors"]:
        raise UiCustomizeError(
            "refusing to apply — validation failed on re-check: " + "; ".join(revalidated["errors"])
        )

    _ensure_dirs()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_name = f"{target}-{timestamp}-{change_id}{path.suffix}"
    backup_path = BACKUPS_ROOT / backup_name
    backup_path.write_text(original_content, encoding="utf-8")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)

    entry = {
        "entry_id": uuid.uuid4().hex[:16],
        "kind": "apply",
        "target": target,
        "instruction": record["instruction"],
        "explanation": record["explanation"],
        "backup_file": backup_name,
        "actor": actor,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    manifest = _load_manifest()
    manifest.insert(0, entry)
    _save_manifest(manifest)

    pending_path.unlink(missing_ok=True)

    if target == "dashboard":
        _broadcast_static_file_changed()

    return entry


def _broadcast_static_file_changed() -> None:
    """Best-effort — the dashboard route calling apply_change()/
    revert_change() is itself already inside the running server process,
    so this reuses the exact same broadcast helper bot/db.py's listener
    registry uses for job_update/chat_message, called directly rather
    than through that registry (nothing about _broadcast_soon requires
    going through db.py — it's a plain function taking a payload dict)."""
    try:
        from bot.dashboard.server import _broadcast_soon

        _broadcast_soon({"type": "static_file_changed", "target": "dashboard"})
    except Exception:  # pragma: no cover - never let a broadcast failure break apply/revert
        pass


def list_history(target: Optional[str] = None) -> list[dict]:
    entries = _load_manifest()
    if target:
        entries = [e for e in entries if e["target"] == target]
    return entries


def revert_change(entry_id: str, *, actor: str = "dashboard") -> dict:
    manifest = _load_manifest()
    record = next((e for e in manifest if e["entry_id"] == entry_id), None)
    if record is None:
        raise UiCustomizeError(f"no history entry {entry_id!r}")

    backup_path = BACKUPS_ROOT / record["backup_file"]
    if not backup_path.exists():
        raise UiCustomizeError(f"backup file for entry {entry_id!r} is missing on disk — cannot revert")
    backup_content = backup_path.read_text(encoding="utf-8")

    target = record["target"]
    path = _target_path(target)
    current_content = path.read_text(encoding="utf-8") if path.exists() else ""

    _ensure_dirs()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    pre_revert_backup_name = f"{target}-{timestamp}-prerevert-{entry_id}{path.suffix}"
    (BACKUPS_ROOT / pre_revert_backup_name).write_text(current_content, encoding="utf-8")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(backup_content, encoding="utf-8")
    tmp.replace(path)

    new_entry = {
        "entry_id": uuid.uuid4().hex[:16],
        "kind": "revert",
        "target": target,
        "instruction": f"revert of {entry_id}",
        "explanation": f"Reverted \"{record['instruction']}\" back to its pre-change state.",
        "backup_file": pre_revert_backup_name,
        "actor": actor,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    manifest.insert(0, new_entry)
    _save_manifest(manifest)

    if target == "dashboard":
        _broadcast_static_file_changed()

    return new_entry
