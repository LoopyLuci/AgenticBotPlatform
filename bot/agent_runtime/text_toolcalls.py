"""Tool calls that a model wrote as TEXT instead of through the API's `tool_calls` field.

Local models served through llama.cpp, Ollama and the like often put the call in the reply: Qwen and Hermes models
write `<tool_call>{json}</tool_call>`, Qwen3-coder writes `<function=name><parameter=key>value</parameter></function>`,
others print a bare JSON object or a fenced ```json block. This module turns such text into calls the loop can run,
and is deliberately strict about it:

* only a call whose NAME is a tool offered this turn, and whose arguments pass that tool's JSON schema (after light
  coercion: "5" -> 5, "true" -> true, a JSON string where an array is expected), is accepted;
* near-miss JSON is repaired only where the fix is unambiguous: trailing commas, single quotes, raw newlines or tabs
  inside strings, Python True/False/None, unquoted keys, invalid escapes such as `\\d`;
* if ANY call in the text is unusable, none is run (no partial or ambiguous execution) and the errors are returned so
  the transport can ask the model once, with a short corrective message, to try again.

Also here: the per-model counters (native / parsed from text / repaired / failed) shown on the router's model board.

Formats: `<tool_call>` + JSON, `<tool_call>` + `<function=...>`, bare `<function=name>{json}</function>`, a bare JSON
object or array (`name` + `arguments`/`parameters`/`args`/`input`, OpenAI `function` wrapper, `tool_calls` list,
Mistral `[TOOL_CALLS]`), and fenced ```json blocks of the same shapes.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

MODES = ("native", "text", "grammar")
FINAL_ANSWER = "final_answer"          # grammar mode: the pseudo-tool a model uses to answer in words

_NAME_KEYS = ("name", "tool", "tool_name", "function_name")
_ARGS_KEYS = ("arguments", "parameters", "args", "input", "params")


def mode_for(model: str, provider: str = "") -> str:
    """The model's `tool_call_mode` from `native_agent.tool_call_modes` (native | text | grammar; default native).
    A key may be "provider/model", a model name, a prefix of one, or "*"; the longest match wins."""
    try:
        from bot.config import config

        table = (config.current.get("native_agent") or {}).get("tool_call_modes") or {}
    except Exception:  # noqa: BLE001
        return "native"
    if not isinstance(table, dict) or not table:
        return "native"
    name = str(model or "")
    full = f"{provider}/{name}" if provider else name
    best, best_len = "native", -1
    for key, value in table.items():
        k = str(key)
        hit = k == "*" or k == full or k == name or full.startswith(k) or name.startswith(k)
        if hit and len(k) > best_len and str(value).lower() in MODES:
            best, best_len = str(value).lower(), (0 if k == "*" else len(k))
    return best


# ---- counters ---------------------------------------------------------------------------
_lock = threading.Lock()
_counts: dict[str, dict[str, int]] = {}
KINDS = ("native", "text", "repaired", "failed")


def record(ref: str, kind: str, n: int = 1) -> None:
    """Count tool-call outcomes for one model ("provider/model"): native, text (parsed from text), repaired
    (parsed after a JSON fix or the corrective re-ask), failed (text that looked like a call and was given up on)."""
    if kind not in KINDS or n <= 0:
        return
    with _lock:
        row = _counts.setdefault(str(ref), dict.fromkeys(KINDS, 0))
        row[kind] += n


def stats(ref: Optional[str] = None) -> dict:
    """One model's counters, or (no `ref`) every model's."""
    with _lock:
        if ref is None:
            return {k: dict(v) for k, v in _counts.items()}
        return dict(_counts.get(str(ref)) or dict.fromkeys(KINDS, 0))


def reset() -> None:
    with _lock:
        _counts.clear()


# ---- lenient JSON ----------------------------------------------------------------------
_VALID_ESCAPES = '"\\/bfnrtu'
_LITERALS = {"True": "true", "False": "false", "None": "null", "true": "true", "false": "false", "null": "null"}


def _repair(s: str) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    quote: Optional[str] = None
    while i < n:
        c = s[i]
        if quote:
            if c == "\\":
                nxt = s[i + 1] if i + 1 < n else ""
                if nxt == "'":
                    out.append("'")
                elif nxt and nxt in _VALID_ESCAPES:
                    out.append(c + nxt)
                elif nxt:
                    out.append("\\\\" + nxt)         # `\d` in a string means a backslash and a d
                else:
                    out.append("\\\\")
                i += 2
                continue
            if c == quote:
                out.append('"')
                quote = None
            elif c == '"':
                out.append('\\"')                    # a double quote inside a single-quoted string
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            elif ord(c) < 0x20:
                out.append("\\u%04x" % ord(c))
            else:
                out.append(c)
            i += 1
            continue
        if c in "\"'":
            quote = c
            out.append('"')
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                i += 1                               # trailing comma
                continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            word = s[i:j]
            k = j
            while k < n and s[k] in " \t":
                k += 1
            if k < n and s[k] == ":":
                out.append(json.dumps(word))        # unquoted key
            else:
                out.append(_LITERALS.get(word, word))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def lenient_loads(text: str) -> tuple[Any, bool]:
    """(value, repaired). Strict JSON first; else the unambiguous repairs above. Raises ValueError."""
    text = text.strip()
    try:
        return json.loads(text), False
    except ValueError:
        pass
    return json.loads(_repair(text)), True


def _balanced_end(text: str, start: int) -> Optional[int]:
    """Index after the bracket that closes the one at `start`, or None. Quotes (either kind) hide brackets."""
    depth, i, n = 0, start, len(text)
    quote: Optional[str] = None
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


# ---- the schema: coercion and validation ----------------------------------------------------
def _type_ok(t: str, v: Any) -> bool:
    return {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "array": isinstance(v, list), "object": isinstance(v, dict), "null": v is None}.get(t, True)


def coerce(schema: Any, value: Any) -> Any:
    """Light coercion toward `schema`. Never invents data: a value that does not fit stays as it is, and validation
    reports it."""
    if not isinstance(schema, dict):
        return value
    t = schema.get("type")
    types = [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]
    if not types:
        return value
    if not isinstance(value, (list, dict)) and any(_type_ok(x, value) for x in types):
        return value                                  # a scalar that already fits
    if isinstance(value, str):
        v = value.strip()
        if "integer" in types and re.fullmatch(r"[+-]?\d+", v):
            return int(v)
        if "number" in types and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", v):
            f = float(v)
            return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
        if "boolean" in types and v.lower() in ("true", "false"):
            return v.lower() == "true"
        if ("array" in types and v.startswith("[")) or ("object" in types and v.startswith("{")):
            try:
                loaded, _ = lenient_loads(v)
            except ValueError:
                return value
            return coerce(schema, loaded)
        return value
    if isinstance(value, float) and "integer" in types and value.is_integer():
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "string" in types \
            and "number" not in types and "integer" not in types:
        return str(value)
    if isinstance(value, list) and "array" in types:
        items = schema.get("items")
        return [coerce(items, x) for x in value] if isinstance(items, dict) else value
    if isinstance(value, dict) and "object" in types:
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = set(schema.get("required") or [])
        out = {}
        for k, v in value.items():
            if v is None and k not in required and k in props and "null" not in _types_of(props[k]):
                continue                              # an explicit null for an optional field: leave it out
            out[k] = coerce(props.get(k), v)
        return out
    return value


def _types_of(schema: Any) -> list:
    t = schema.get("type") if isinstance(schema, dict) else None
    return [t] if isinstance(t, str) else list(t or [])


def validate(schema: Optional[dict], args: Any) -> tuple[Any, Optional[str]]:
    """(coerced args, None) or (args, a short error)."""
    schema = schema or {"type": "object", "properties": {}}
    coerced = coerce(schema, args)
    try:
        from jsonschema import Draft202012Validator

        errors = sorted(Draft202012Validator(schema).iter_errors(coerced), key=lambda e: list(e.absolute_path))
    except Exception:  # noqa: BLE001 - a tool whose own schema is broken: check the basics only
        if not isinstance(coerced, dict):
            return args, "the arguments must be a JSON object"
        missing = [k for k in (schema.get("required") or []) if k not in coerced]
        return coerced, (f"missing required argument(s): {', '.join(missing)}" if missing else None)
    if not errors:
        return coerced, None
    parts = []
    for e in errors[:3]:
        where = ".".join(str(p) for p in e.absolute_path)
        parts.append(f"{where}: {e.message}"[:200] if where else e.message[:200])
    return args, "; ".join(parts)


# ---- finding the calls in a text ---------------------------------------------------------------
@dataclass
class TextCall:
    name: str
    arguments: dict
    format: str


@dataclass
class ParseResult:
    calls: list[TextCall] = field(default_factory=list)
    text: str = ""                                    # the reply without the call text (the original when nothing is accepted)
    errors: list[str] = field(default_factory=list)
    attempted: bool = False                           # the text looked like an attempt at a call
    repaired: bool = False                            # some JSON needed a fix
    final_answer: Optional[str] = None                # grammar mode: the model answered in words


_TOOL_CALL = re.compile(r"<tool_call>(.*?)</tool_call>", re.S | re.I)
_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.S)
_PARAM = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.S)
_FENCE = re.compile(r"```[ \t]*(?:json|JSON|tool_call|tool|javascript)?[ \t]*\r?\n(.*?)```", re.S)
_MISTRAL = re.compile(r"\[TOOL_CALLS\]\s*$")


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = "\0"
    return "".join(chars)


def _json_calls(value: Any, explicit: bool) -> Optional[list[tuple[str, Any, Optional[str]]]]:
    """[(name, raw args, error)] when `value` has the shape of a call or a list of calls, else None. `explicit`: the
    text was marked as a call (inside <tool_call>), so a name without arguments is a call with none."""
    if isinstance(value, list):
        found = [_json_calls(v, explicit) for v in value]
        if not any(found):
            return None
        out: list[tuple[str, Any, Optional[str]]] = []
        for i, f in enumerate(found):
            out.extend(f or [("", None, f"item {i + 1} is not a tool call")])
        return out
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("tool_calls"), list):
        return _json_calls(value["tool_calls"], explicit)
    fn = value.get("function")
    if isinstance(fn, dict):
        return _json_calls(fn, True)
    name = next((value[k] for k in _NAME_KEYS if isinstance(value.get(k), str)), None)
    if isinstance(fn, str) and name is None:
        name = fn
    arg_key = next((k for k in _ARGS_KEYS if k in value), None)
    if name is None or (arg_key is None and not explicit):
        return None
    return [(name, value.get(arg_key) if arg_key else {}, None)]


def _xml_args(body: str) -> tuple[dict, bool]:
    """Arguments of a `<function=...>` body: `<parameter=k>v</parameter>` pairs, else one JSON object."""
    params = _PARAM.findall(body)
    if params:
        args: dict = {}
        for k, v in params:
            v = v.strip("\r\n")
            if v.startswith("\n"):
                v = v[1:]
            try:                                      # a value written as JSON (number, list...) is taken as such
                loaded, _ = lenient_loads(v)
                args[k] = loaded if isinstance(loaded, (list, dict)) else v
            except ValueError:
                args[k] = v
        return args, False
    stripped = body.strip()
    if not stripped:
        return {}, False
    loaded, repaired = lenient_loads(stripped)
    if not isinstance(loaded, dict):
        raise ValueError("the arguments must be a JSON object")
    return loaded, repaired


def _resolve(name: str, tools: dict[str, dict]) -> Optional[str]:
    n = str(name or "").strip()
    for prefix in ("functions.", "default_api.", "functions/"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n in tools:
        return n
    low = [t for t in tools if t.lower() == n.lower()]
    return low[0] if len(low) == 1 else None


def parse(text: str, tool_schemas: list[dict], *, allow_final: bool = False) -> ParseResult:
    """Find the tool calls written as text in `text`. `tool_schemas`: the Anthropic-shaped schemas offered this turn
    (name, input_schema). `allow_final`: grammar mode, where `{"name": "final_answer", "arguments": {"text": ...}}`
    is the model's way of answering in words."""
    result = ParseResult(text=text or "")
    if not text or not tool_schemas:
        return result
    tools = {str(s.get("name")): s for s in tool_schemas if s.get("name")}
    spans: list[tuple[int, int]] = []
    raw: list[tuple[str, Any, Optional[str], str]] = []   # (name, args, error, format) per found call
    repaired = False

    def add_json(payload: str, fmt: str, explicit: bool) -> bool:
        """Parse a JSON payload; True when it was call-shaped (an attempt)."""
        nonlocal repaired
        try:
            value, fixed = lenient_loads(payload)
        except ValueError as e:
            if explicit:
                raw.append(("", None, f"the {fmt} text is not valid JSON ({str(e)[:80]})", fmt))
                return True
            return False
        shaped = _json_calls(value, explicit)
        if shaped is None:
            if explicit:
                raw.append(("", None, f"the {fmt} text is not a call: it needs a name and arguments", fmt))
                return True
            return False
        repaired = repaired or fixed
        for name, args, err in shaped:
            raw.append((name, args, err, fmt))
        return True

    def add_function(name: str, body: str, fmt: str) -> None:
        nonlocal repaired
        try:
            args, fixed = _xml_args(body)
        except ValueError as e:
            raw.append((name, None, f"arguments of {name} are not valid ({str(e)[:80]})", fmt))
            return
        repaired = repaired or fixed
        raw.append((name, args, None, fmt))

    # 1. <tool_call> ... </tool_call>
    for m in _TOOL_CALL.finditer(text):
        spans.append((m.start(), m.end()))
        body = m.group(1)
        fm = _FUNCTION.search(body)
        if fm:
            add_function(fm.group(1), fm.group(2), "tool_call+function")
        else:
            add_json(body, "tool_call", True)
    rest = _mask(text, spans)
    # an opening tag that never closes: a cut-off call, never run
    for m in re.finditer(r"<tool_call>", rest, re.I):
        raw.append(("", None, "a <tool_call> was opened and never closed", "tool_call"))
        spans.append((m.start(), len(text)))
        break
    rest = _mask(text, spans)
    # 2. <function=name> ... </function>
    for m in _FUNCTION.finditer(rest):
        spans.append((m.start(), m.end()))
        add_function(m.group(1), m.group(2), "function")
    rest = _mask(text, spans)
    for m in re.finditer(r"<function=([^>\s]+)>", rest):
        raw.append((m.group(1), None, f"<function={m.group(1)}> was opened and never closed", "function"))
        spans.append((m.start(), len(text)))
        break
    rest = _mask(text, spans)
    # 3. fenced blocks
    for m in _FENCE.finditer(rest):
        body = m.group(1)
        before = len(raw)
        if add_json(body, "json block", False):
            spans.append((m.start(), m.end()))
        else:
            del raw[before:]
    rest = _mask(text, spans)
    # 4. bare JSON objects and arrays
    i = 0
    while i < len(rest):
        if rest[i] not in "{[":
            i += 1
            continue
        end = _balanced_end(rest, i)
        if end is None or "\0" in rest[i:end]:
            i += 1
            continue
        before = len(raw)
        if add_json(rest[i:end], "JSON", False):
            start = i
            mm = _MISTRAL.search(rest[:i])
            if mm:
                start = mm.start()
            spans.append((start, end))
            i = end
        else:
            del raw[before:]
            i += 1

    if not raw:
        return result
    result.attempted = True
    result.repaired = repaired
    calls: list[TextCall] = []
    errors: list[str] = []
    final: Optional[str] = None
    for name, args, err, fmt in raw:
        if err:
            errors.append(err)
            continue
        if allow_final and str(name).strip() == FINAL_ANSWER:
            if isinstance(args, str):
                try:
                    args, _ = lenient_loads(args)
                except ValueError:
                    pass
            final = str(args.get("text", "")) if isinstance(args, dict) else str(args or "")
            continue
        resolved = _resolve(name, tools)
        if resolved is None:
            errors.append(f"there is no tool named {str(name)[:60]!r}")
            continue
        if isinstance(args, str):
            try:
                args, fixed = lenient_loads(args) if args.strip() else ({}, False)
                result.repaired = result.repaired or fixed
            except ValueError:
                errors.append(f"arguments of {resolved} are not valid JSON")
                continue
        if args is None:
            args = {}
        if not isinstance(args, dict):
            errors.append(f"arguments of {resolved} must be a JSON object")
            continue
        fixed_args, verr = validate(tools[resolved].get("input_schema"), args)
        if verr:
            errors.append(f"arguments of {resolved}: {verr}")
            continue
        calls.append(TextCall(resolved, fixed_args, fmt))
    if errors:
        result.errors = errors                        # nothing runs: the text stays as it was
        return result
    cleaned = text
    for a, b in sorted(spans, reverse=True):
        cleaned = cleaned[:a] + cleaned[b:]
    result.text = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    result.calls = calls
    result.final_answer = final
    if final is not None and not calls:
        result.text = final
    return result


def call_id() -> str:
    return "call_text_" + uuid.uuid4().hex[:12]


def corrective_message(errors: list[str], tool_schemas: list[dict], *, grammar: bool = False) -> str:
    names = ", ".join(str(s.get("name")) for s in tool_schemas if s.get("name"))[:600]
    shown = "; ".join(errors)[:600]
    if grammar:
        form = ('Reply with one JSON object: {"name": "<tool>", "arguments": {...}}, or '
                '{"name": "final_answer", "arguments": {"text": "<your answer>"}}.')
    else:
        form = 'Call a tool as <tool_call>{"name": "<tool>", "arguments": {...}}</tool_call>, or answer in plain text if no tool is needed.'
    return (f"That was not a usable tool call: {shown}. Tools you may call: {names}. {form} "
            "Do not repeat the same mistake.")


def tools_prompt(tool_schemas: list[dict], *, grammar: bool = False) -> str:
    """The system-prompt section for `text` and `grammar` modes, where the API's own tools field is not used."""
    lines = ["You can call these tools. Each is described by its name, purpose and JSON schema of its arguments:"]
    for s in tool_schemas:
        lines.append(json.dumps({"name": s.get("name"), "description": str(s.get("description") or "")[:400],
                                 "parameters": s.get("input_schema") or {"type": "object", "properties": {}}},
                                separators=(",", ":")))
    if grammar:
        lines.append('Every reply is one JSON object. To call a tool: {"name": "<tool>", "arguments": {...}}. '
                     'To answer in words (when no tool is needed): {"name": "final_answer", "arguments": {"text": "<answer>"}}.')
    else:
        lines.append('To call a tool, write exactly: <tool_call>{"name": "<tool>", "arguments": {...}}</tool_call> '
                     "and stop. You may write several such blocks. Use only the tools above. When no tool is needed, answer in plain text.")
    return "\n".join(lines)


def grammar_schema(tool_schemas: list[dict]) -> dict:
    """A JSON schema for the whole reply in `grammar` mode: one tool call, or final_answer with the text."""
    options = [{"type": "object", "properties": {"name": {"const": FINAL_ANSWER},
                                                 "arguments": {"type": "object", "properties": {"text": {"type": "string"}},
                                                               "required": ["text"]}},
                "required": ["name", "arguments"]}]
    for s in tool_schemas:
        if s.get("name"):
            options.append({"type": "object", "properties": {"name": {"const": s["name"]},
                                                             "arguments": s.get("input_schema") or {"type": "object"}},
                            "required": ["name", "arguments"]})
    return {"anyOf": options}
