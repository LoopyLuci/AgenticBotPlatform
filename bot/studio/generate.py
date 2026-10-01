"""Layer 2: models propose changes to ABP's UI, each as its own variant.

One request (an instruction, the files it concerns, and optionally a focus: a section id or a word to centre long
files on) goes to several model attempts at once (different models, or the same model asked for different design
directions). Each answer is a set of edit blocks (bot/studio/edits.py), applied to a fresh variant exactly or refused,
then validated: the HTML parses and balances, the JavaScript passes `node --check`. All of it is logged (bot/studio/log)
with the request, the model and the outcome, which is what the datasets are made of.

Models: the ones named, else ABP's configured free models and its local ones (ModelMistress, Ollama), never a paid
default: with none configured it says so."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Optional

from bot.studio import edits, log, variants

DIRECTIONS = ("", "Keep the change minimal and consistent with the existing style.",
              "Make it visually bolder and more distinctive, still within the existing design tokens.",
              "Favour compactness and information density.", "Favour clarity and generous spacing.")
# models that do not write code (music, speech, images, embeddings, moderation): never offered a UI change
_NOT_TEXT = re.compile(r"lyria|tts|whisper|speech|audio|image|imagen|flux|stable-diffusion|sdxl|embed|rerank|moderation|"
                       r"guard|vision-only|ocr", re.I)
MAX_ATTEMPTS_PER_VARIANT = 3          # a failed call (rate limit, timeout) moves on to the next model, up to this many
MAX_CONTEXT_CHARS = 90_000
PER_CALL_TIMEOUT_S = 420.0

PROMPT = """You change the user interface of AgenticBotPlatform (ABP), a local web app (HTML, CSS, vanilla JavaScript).

Request: {instruction}
{direction}
Answer ONLY with edit blocks, then one EXPLANATION line. To change a file:

FILE: <path exactly as given below>
<<<<<<< SEARCH
<lines copied exactly from the file, enough to be unique>
=======
<the new lines>
>>>>>>> REPLACE

To add a file (under bot/dashboard/static/ or desktop-app/ui/):

NEW FILE: <path>
```
<the whole file>
```

EXPLANATION: <one or two sentences>

Rules: SEARCH text must be copied exactly from the file shown. Keep the page's existing structure, ids, scripts and the
theme's CSS variables (var(--accent), var(--surface)...). No inline <script> in HTML (the pages' security policy blocks
it): behaviour goes in .js files. No external URLs.

The files:
{files}
"""


def _excerpt(text: str, focus: str, keep: int) -> str:
    """A long file centred on `focus` (a section id or a word), with line numbers dropped: what fits in `keep`."""
    if len(text) <= keep:
        return text
    lines = text.split("\n")
    hits = [i for i, ln in enumerate(lines) if focus and focus.lower() in ln.lower()] if focus else []
    if not hits:
        return text[:keep] + "\n... (the rest of the file is not shown)"
    out, used, shown = [], 0, set()
    for h in hits:
        for i in range(max(0, h - 60), min(len(lines), h + 120)):
            if i not in shown and used < keep:
                shown.add(i)
                used += len(lines[i]) + 1
    prev = -2
    for i in sorted(shown):
        if i != prev + 1:
            out.append("... (lines not shown) ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def build_prompt(instruction: str, files: list[str], focus: str = "", direction: str = "",
                 base: Optional[str] = None) -> str:
    per = max(4_000, MAX_CONTEXT_CHARS // max(1, len(files)))
    shown = []
    for f in files:
        r = variants.check_rel(f)
        text = variants.read(base, r)
        shown.append(f"--- FILE: {r} ---\n{_excerpt(text, focus, per)}\n--- END {r} ---")
    return PROMPT.format(instruction=instruction.strip(), direction=f"Design direction: {direction}\n" if direction else "",
                         files="\n\n".join(shown))


async def candidates(explicit: Optional[list[str]] = None) -> list[tuple[Optional[str], str]]:
    """(provider, model) pairs to try: those given as "provider/model", else ABP's free and local models."""
    if explicit:
        out = []
        for ref in explicit:
            if "/" not in ref:
                raise variants.StudioError(f"{ref!r}: give models as provider/model")
            p, m = ref.split("/", 1)
            out.append((p, m))
        return out
    from bot import providers as prov
    from bot.models import custom_models_with_pricing
    try:
        priced, _src = await custom_models_with_pricing()
    except Exception:  # noqa: BLE001
        priced = {}
    free = [(p, e["id"]) for p in sorted(prov.list_providers()) for e in sorted(priced.get(p, []), key=lambda e: e["id"])
            if e.get("free") and not _NOT_TEXT.search(e["id"])]
    local = [(p, "auto") for p, cfg in prov.module_providers().items() if p in ("modelmistress",)]
    found = free + local
    if not found:
        raise variants.StudioError("no free or local model is configured (Settings, Providers): Studio does not fall "
                                   "back to a paid model on its own; name one with models=[\"provider/model\"]")
    return found


def validate(vid: str) -> list[str]:
    """What is wrong with a variant's changed files (empty = nothing found)."""
    from bot import ui_customize as uc
    problems = []
    for r in variants.files(vid):
        text = variants.read(vid, r)
        if r.endswith(".html"):
            problems += [f"{r}: {e}" for e in uc._check_html_balance(text)]
            for js in uc._extract_script_blocks(text):
                ran, err = uc._check_js_syntax(js)
                if err:
                    problems.append(f"{r}: inline script: {err}")
        elif r.endswith(".js"):
            ran, err = uc._check_js_syntax(text)
            if err:
                problems.append(f"{r}: {err}")
            elif not ran and (bal := uc._check_js_balance(text)):
                problems.append(f"{r}: {bal}")
    return problems


async def _one(req: dict, chain: list[tuple[Optional[str], str]], direction: str, group: str, n: int) -> dict:
    """One variant: its models in order (a failed call moves on to the next); the variant is made only once a model
    has answered, so a call that failed never looks like a proposal someone rejected."""
    from bot.agent_runtime.moa import _single_call
    t0 = time.time()
    prompt = build_prompt(req["instruction"], req["files"], req.get("focus", ""), direction, req.get("base"))
    raw, provider, model, failures = None, None, "", []
    for provider, model in chain[:MAX_ATTEMPTS_PER_VARIANT]:
        try:
            raw = await asyncio.wait_for(_single_call(provider, model, prompt, max_tokens=16000,
                                                      timeout_s=PER_CALL_TIMEOUT_S), timeout=PER_CALL_TIMEOUT_S + 30)
            break
        except Exception as e:  # noqa: BLE001 - a rate limit or a timeout: the next model takes over
            failures.append(f"{provider}/{model}: {str(e)[:300]}")
            log.event("generate-failed", group=group, instruction=req["instruction"], model=f"{provider}/{model}",
                      error=str(e)[:500])
    if raw is None:
        return {"model": " → ".join(f"{p}/{m}" for p, m in chain[:MAX_ATTEMPTS_PER_VARIANT]), "ok": False,
                "error": "every model failed: " + " | ".join(failures)[:1500]}
    v = variants.create(f"{req['title']} · {model.split('/')[-1][:24]}" + (f" · {n + 1}" if n else ""),
                        note=req["instruction"], base=req.get("base") or "", group=group, origin="model")
    vid = v["id"]
    parsed = edits.parse(raw)
    refused = []
    for f, search, replace in parsed.edits:
        try:
            r = variants.check_rel(f)
            cur = variants.read(vid, r)
        except variants.StudioError as e:
            refused.append(f"{f}: {e}")
            continue
        new, why = edits.apply(cur, search, replace)
        if why:
            refused.append(f"{r}: {why}")
            continue
        variants.write(vid, r, new, why="generate-edit", source=f"{provider}/{model}")
    for f, content in parsed.new_files.items():
        try:
            variants.write(vid, variants.check_rel(f), content, why="generate-edit", source=f"{provider}/{model}")
        except variants.StudioError as e:
            refused.append(f"{f}: {e}")
    problems = validate(vid) if variants.files(vid) else ["no edit could be applied"]
    rec = {"model": f"{provider}/{model}", "variant": vid, "ok": not problems, "explanation": parsed.explanation,
           "edits": len(parsed.edits) + len(parsed.new_files), "refused": refused, "problems": problems,
           "seconds": round(time.time() - t0, 1), "passed_over": failures}
    log.event("generate", vid=vid, group=group, instruction=req["instruction"], files=req["files"],
              model=f"{provider}/{model}", direction=direction, ok=not problems, explanation=parsed.explanation,
              refused=refused, problems=problems, seconds=rec["seconds"], raw=raw[:30000])
    return rec


async def propose(instruction: str, files: list[str], *, variants_wanted: int = 3, models: Optional[list[str]] = None,
                  focus: str = "", title: str = "", base: str = "") -> dict:
    """Up to `variants_wanted` variants for one request, made at once; returns each attempt's outcome."""
    if not instruction.strip():
        raise variants.StudioError("say what to change")
    if not files:
        raise variants.StudioError("name the files it concerns (e.g. bot/dashboard/static/vision-panel.js)")
    files = [variants.check_rel(f) for f in files]
    n = max(1, min(int(variants_wanted), 6))
    cands = await candidates(models)
    group = uuid.uuid4().hex[:10]
    req = {"instruction": instruction, "files": files, "focus": focus, "base": base or None,
           "title": (title or re.sub(r"\s+", " ", instruction))[:40]}
    # variant i starts with candidate i and falls back to the ones after it; with fewer models than variants, the
    # extra ones get a design direction so they differ
    plan = [([cands[(i + k) % len(cands)] for k in range(len(cands))],
             DIRECTIONS[i % len(DIRECTIONS)] if len(cands) < n or i else "") for i in range(n)]
    results = await asyncio.gather(*(_one(req, chain, d, group, i) for i, (chain, d) in enumerate(plan)))
    return {"group": group, "instruction": instruction, "files": files, "attempts": results,
            "variants": [r["variant"] for r in results if r.get("variant")]}
