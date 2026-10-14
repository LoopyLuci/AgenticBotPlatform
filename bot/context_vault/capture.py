"""Passive capture: the context API models work with, kept locally without asking anything of the turn.

Every hook (`on_compaction`, `on_turn_end`, `on_board_event`, `on_step_result`) does only cheap checks and a shallow
copy on the caller's thread, then queues the real work (extraction, redaction, embedding, storing) for the vault's
worker thread. Nothing here raises into the caller, and with `context_vault.enabled` false nothing happens at all.

Extraction is heuristic (sentences classified by wording, error -> fix pairs from tool results, the compaction
summary kept whole as task state). A configured LOCAL model (`summarise.enabled`) can turn the text into items
instead; if it is down the heuristics run.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from bot.context_vault import settings as vsettings

logger = logging.getLogger("bot.context_vault")

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|md|ya?ml|json|toml|rs|go|java|c|cc|cpp|h|hpp|sql|html|css|sh|ps1))\b")
_ERR = re.compile(r"\b(error|exception|traceback|failed|failure|not found|denied|cannot|can't|timed out)\b", re.I)
_FIX = re.compile(r"\b(fix(?:ed)?|resolv(?:ed|e)|solved|the problem was|root cause|worked|now passes|the solution|workaround)\b", re.I)
_RULE = re.compile(r"(?:^|\b)(?:rule:|never\b|must not\b|must always\b|do not ever\b|always\b)", re.I)
_PREF = re.compile(r"\b(?:prefers?|preferred|likes? to|wants? (?:it|them|this) to|would rather)\b", re.I)
_DECISION = re.compile(r"\b(?:decided|decision|we(?:'ll| will) use|chose|chosen|agreed|going with|settled on|will use)\b", re.I)
_PLAN = re.compile(r"\b(?:plan:|the plan|next steps?|to-?do|will then|first,? .{0,80}then|step \d)\b", re.I)
_DESCRIBES = re.compile(r"\b(?:handles|contains|defines|implements|holds|provides|is responsible|exports|loads|reads|writes|is the|wraps)\b", re.I)
KINDS_ORDER = ("rule", "preference", "decision", "plan", "file_summary")


def _cfg() -> dict:
    return vsettings.load()


def _active(section: str) -> Optional[dict]:
    """The settings when the vault is on and this capture source is allowed, else None. Cheap."""
    cfg = _cfg()
    if not cfg.get("enabled"):
        return None
    cap = cfg.get("capture") or {}
    if section and not cap.get(section, True):
        return None
    return cfg


def _is_local(transport: Any) -> bool:
    try:
        from bot.agent_runtime import turn_budget

        return bool(turn_budget.is_local(transport))
    except Exception:  # noqa: BLE001
        return False


def _api_turn_allowed(cfg: dict, transport: Any) -> bool:
    cap = cfg.get("capture") or {}
    return bool(cap.get("local_turns")) if _is_local(transport) else bool(cap.get("api_turns", True))


def _project(cwd: Any) -> str:
    if not cwd:
        return ""
    return re.split(r"[\\/]", str(cwd).rstrip("\\/"))[-1][:80]


def _vault():
    from bot.context_vault import get_vault

    return get_vault()


def _submit(fn) -> None:
    v = _vault()
    if v is not None:
        v.submit(fn)


# ---------------------------------------------------------------------------------------------------- extraction
def sentences(text: str) -> list[str]:
    out = []
    for part in _SPLIT.split(text or ""):
        s = _BULLET.sub("", part).strip()
        if 20 <= len(s) <= 400 and not s.endswith("?"):
            out.append(s)
    return out


def classify(sentence: str) -> Optional[str]:
    if _RULE.search(sentence):
        return "rule"
    if _PREF.search(sentence):
        return "preference"
    if _DECISION.search(sentence):
        return "decision"
    if _PLAN.search(sentence):
        return "plan"
    if _PATH.search(sentence) and _DESCRIBES.search(sentence):
        return "file_summary"
    return None


def extract_from_text(text: str, limit: int = 20) -> list[dict]:
    """[{"kind", "text", "paths"}] from prose such as a compaction summary or a final answer."""
    items: list[dict] = []
    sents = sentences(text)
    for i, s in enumerate(sents):
        kind = classify(s)
        if kind:
            items.append({"kind": kind, "text": s, "paths": _PATH.findall(s)[:5]})
            continue
        nxt = sents[i + 1] if i + 1 < len(sents) else ""
        both = _ERR.search(s) and _FIX.search(s)
        if both:
            items.append({"kind": "error_fix", "text": s, "paths": _PATH.findall(s)[:5]})
        elif _ERR.search(s) and _FIX.search(nxt):
            items.append({"kind": "error_fix", "text": f"Error: {s}\nFix: {nxt}", "paths": _PATH.findall(s + " " + nxt)[:5]})
    return items[:limit]


def extract_from_history(messages: list[dict], limit: int = 12) -> list[dict]:
    """Error -> fix pairs from tool results and the assistant text after them, plus the user's stated preferences."""
    from bot.agent_runtime import session_export

    items: list[dict] = []
    pending: Optional[str] = None
    for turn in session_export.normalise(messages):
        for part in turn["parts"]:
            kind, text = part.get("kind"), str(part.get("text") or "")
            if kind == "tool_result" and _ERR.search(text[:400]):
                pending = " ".join(text.split())[:300]
            elif kind == "text" and turn["role"] == "assistant" and pending and _FIX.search(text):
                fix = " ".join(text.split())[:400]
                items.append({"kind": "error_fix", "text": f"Error: {pending}\nFix: {fix}", "paths": _PATH.findall(fix)[:5]})
                pending = None
            elif kind == "text" and turn["role"] == "user":
                items.extend(_user_statements(text))
    return items[:limit]


def _user_statements(text: str) -> list[dict]:
    try:
        from bot.memoryfabric import extract
    except Exception:  # noqa: BLE001
        return []
    out = []
    for c in extract.candidates(text)[:4]:
        content = str(c.get("content") or "").strip()
        if not content:
            continue
        kind = "rule" if re.match(r"(?i)(never|always)\b", content) else "preference" if c.get("kind") in ("feedback", "user") else "note"
        out.append({"kind": kind, "text": content, "paths": []})
    return out


def _llm_items(cfg: dict, text: str) -> Optional[list[dict]]:
    """Ask the configured LOCAL model for items; None when it is off or fails (the heuristics then run)."""
    s = cfg.get("summarise") or {}
    if not s.get("enabled") or not s.get("model"):
        return None
    try:
        from bot.modelmistress import client

        prompt = ("Extract durable facts worth remembering from the text. Reply with only a JSON list of "
                  '{"kind": one of decision|plan|file_summary|error_fix|preference|rule, "text": one sentence}. '
                  "Never include credentials.\n\n" + text[:6000])
        r = client.chat(str(s["model"]), [{"role": "user", "content": prompt}], timeout=60.0)
        body = (((r.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        data = json.loads(body[body.index("["): body.rindex("]") + 1])
        return [{"kind": str(d.get("kind") or "note"), "text": str(d.get("text") or ""), "paths": _PATH.findall(str(d.get("text") or ""))[:5]}
                for d in data if isinstance(d, dict) and d.get("text")]
    except Exception as exc:  # noqa: BLE001 - optional
        logger.debug("context vault: local summarisation unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------------------------------- processing
def _store(vault, items: list[dict], source: str, project: str, tags: tuple = ()) -> dict:
    res = {"stored": 0, "deduped": 0, "skipped": 0}
    for it in items:
        r = vault.add(it["text"], it["kind"], source=source, project=project, paths=it.get("paths") or (), tags=tags)
        res[r["status"]] = res.get(r["status"], 0) + 1
    return res


def process_compaction(vault, digest: str, source: str, project: str) -> dict:
    items = _llm_items(vault.cfg, digest) or extract_from_text(digest)
    if len(digest.strip()) >= 40:
        items.append({"kind": "task_state", "text": digest.strip()[:3000], "paths": _PATH.findall(digest)[:8]})
    return _store(vault, items, source, project, tags=("compaction",))


def process_turn(vault, messages: list[dict], final_text: str, source: str, project: str) -> dict:
    items = extract_from_history(messages)
    items += extract_from_text(final_text, limit=8)
    return _store(vault, items, source, project, tags=("turn",))


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return _text_of(parsed)
        except ValueError:
            return value
        return value
    if isinstance(value, dict):
        for key in ("summary", "text", "result", "output", "message"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
    try:
        return json.dumps(value, default=str)[:2000]
    except (TypeError, ValueError):
        return str(value)[:2000]


def process_board(vault, event: str, task: dict) -> dict:
    title = str(task.get("title") or "").strip()
    if not title:
        return {}
    board = str(task.get("board") or "")
    tags = ("board", event) + ((str(task["key"]),) if task.get("key") else ())
    source = f"board:{board}/task:{str(task.get('id') or '')[:8]}"
    if event == "created":
        text, kind = f"Brief: {title}\n{task.get('detail') or ''}".strip(), "brief"
    else:
        body = "\n".join(x for x in (_text_of(task.get("result")), str(task.get("note") or "")) if x.strip())
        if not body:
            return {}
        kind = "review" if event == "review" else "task_state"
        text = f"Task {title} ({event}): {body}"
    r = vault.add(text, kind, source=source, project="", tags=tags)
    return {r["status"]: 1}


# ---------------------------------------------------------------------------------------------------- hooks
def on_compaction(session_key: str, transport: Any, model: str, digest: str, *, instance_id: Any = None, cwd: Any = None) -> None:
    try:
        cfg = _active("compaction")
        if cfg is None or not digest or not _api_turn_allowed(cfg, transport):
            return
        source, project = f"instance:{instance_id}/session:{session_key}/model:{model}", _project(cwd)
        _submit(lambda: process_compaction(_vault(), digest, source, project))
    except Exception:  # noqa: BLE001 - capture never breaks a turn
        logger.debug("context vault: compaction hook failed", exc_info=True)


def on_turn_end(history: list[dict], transport: Any, model: str, final_text: str, *, session_key: str = "",
                instance_id: Any = None, cwd: Any = None) -> None:
    try:
        cfg = _active("turn_end")
        if cfg is None or not _api_turn_allowed(cfg, transport):
            return
        window = list(history[-16:])
        source, project = f"instance:{instance_id}/session:{session_key}/model:{model}", _project(cwd)
        _submit(lambda: process_turn(_vault(), window, final_text or "", source, project))
    except Exception:  # noqa: BLE001
        logger.debug("context vault: turn hook failed", exc_info=True)


def on_board_event(event: str, task: Any) -> None:
    try:
        if _active("board") is None or not isinstance(task, dict):
            return
        snap = dict(task)
        _submit(lambda: process_board(_vault(), event, snap))
    except Exception:  # noqa: BLE001
        logger.debug("context vault: board hook failed", exc_info=True)


def on_step_result(goal: str, status: str, text: str, *, model: str = "", index: Optional[int] = None) -> None:
    try:
        if _active("swarm") is None or not (text or "").strip():
            return
        body = f"Step {'' if index is None else index}({status}) goal: {goal[:300]}\nResult: {text[:1500]}"
        _submit(lambda: _vault().add(body, "task_state", source=f"swarm/model:{model}", tags=("swarm", status)))
    except Exception:  # noqa: BLE001
        logger.debug("context vault: step hook failed", exc_info=True)
