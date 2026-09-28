"""The toolkit's registry: every action, described once, served to agents, the command line and MCP.

An action is a plain function in one of the toolkit's modules, registered with ``@action("group.name", ...)``. Its
parameters (from type hints and defaults) become a JSON Schema; its docstring's first line becomes its summary. Flags:

* ``writes``   it creates or changes files (inside the working folder it is given);
* ``executes`` it runs a program or code;
* ``network``  it reaches the internet.

Agents get one tool per group (``toolkit_lint``, ``toolkit_asset``...) with an ``action`` argument, so the whole kit
costs a dozen tool definitions rather than a hundred.
"""
from __future__ import annotations

import dataclasses
import enum
import inspect
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints


class ToolkitError(Exception):
    """A problem worth showing the caller as it is (bad input, missing program, file outside the workspace)."""


@dataclass
class Action:
    id: str
    group: str
    summary: str
    params: dict[str, Any]
    fn: Callable[..., Any]
    writes: bool = False
    executes: bool = False
    network: bool = False
    needs: list[str] = field(default_factory=list)    # optional programs that make it better (ruff, node, dot...)

    @property
    def read_only(self) -> bool:
        return not (self.writes or self.executes or self.network)

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "group": self.group, "summary": self.summary, "params": self.params,
                "writes": self.writes, "executes": self.executes, "network": self.network, "needs": self.needs}


ACTIONS: dict[str, Action] = {}
GROUPS: dict[str, str] = {}


def group(name: str, summary: str) -> None:
    GROUPS[name] = summary


def action(action_id: str, *, writes: bool = False, executes: bool = False, network: bool = False,
           needs: Optional[list[str]] = None) -> Callable[[Callable], Callable]:
    def wrap(fn: Callable) -> Callable:
        grp = action_id.split(".", 1)[0]
        ACTIONS[action_id] = Action(action_id, grp, _first_line(fn.__doc__), signature_schema(fn), fn,
                                    writes, executes, network, list(needs or []))
        return fn
    return wrap


# ---- schemas from signatures ----------------------------------------------------------------------------------------
_SKIP = {"workspace", "self"}


def _first_line(doc: Optional[str]) -> str:
    return inspect.cleandoc(doc).splitlines()[0].strip() if doc else ""


def _param_docs(doc: Optional[str]) -> dict[str, str]:
    """``name: description`` lines from a docstring's body."""
    out: dict[str, str] = {}
    for line in inspect.cleandoc(doc or "").splitlines()[1:]:
        name, sep, text = line.strip().partition(":")
        if sep and name.isidentifier() and text.strip():
            out[name] = text.strip()
    return out


def type_schema(tp: Any) -> dict[str, Any]:
    if tp is inspect.Parameter.empty or tp is Any:
        return {}
    origin = get_origin(tp)
    if origin is Union or (origin is not None and str(origin) == "types.UnionType"):
        args = [a for a in get_args(tp) if a is not type(None)]
        return type_schema(args[0]) if len(args) == 1 else {"anyOf": [type_schema(a) for a in args]}
    if origin in (list, tuple, set):
        a = get_args(tp)
        return {"type": "array", "items": type_schema(a[0]) if a else {}}
    if origin is dict:
        return {"type": "object"}
    if origin is typing.Literal:
        return {"type": "string", "enum": list(get_args(tp))}
    if isinstance(tp, type):
        if issubclass(tp, bool):
            return {"type": "boolean"}
        if issubclass(tp, enum.Enum):
            return {"type": "string", "enum": [m.value for m in tp]}
        if issubclass(tp, int):
            return {"type": "integer"}
        if issubclass(tp, float):
            return {"type": "number"}
        if issubclass(tp, (str, Path)):
            return {"type": "string"}
        if issubclass(tp, (list, tuple)):
            return {"type": "array"}
        if issubclass(tp, dict):
            return {"type": "object"}
    return {}


def signature_schema(fn: Callable) -> dict[str, Any]:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    docs = _param_docs(fn.__doc__)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if name in _SKIP or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        s = dict(type_schema(hints.get(name, p.annotation)))
        if name in docs:
            s["description"] = docs[name]
        if p.default is inspect.Parameter.empty:
            required.append(name)
        elif p.default is not None:
            s["default"] = p.default
        props[name] = s
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def bind(fn: Callable, args: dict[str, Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    known = {n for n in sig.parameters if n not in _SKIP}
    unknown = set(args) - known
    if unknown:
        raise ToolkitError(f"unknown argument(s): {', '.join(sorted(unknown))}; expected: {', '.join(sorted(known))}")
    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    out = {}
    for name, p in sig.parameters.items():
        if name not in known:
            continue
        if name in args:
            out[name] = _coerce(args[name], hints.get(name))
        elif p.default is inspect.Parameter.empty:
            raise ToolkitError(f"missing required argument: {name}")
    return out


def _coerce(value: Any, tp: Any) -> Any:
    if value is None or tp is None:
        return value
    origin = get_origin(tp)
    if origin is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        return _coerce(value, args[0]) if len(args) == 1 else value
    if isinstance(tp, type):
        if issubclass(tp, bool) and isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if issubclass(tp, int) and not isinstance(value, bool) and isinstance(value, (str, float)):
            return int(value)
        if issubclass(tp, float) and isinstance(value, (str, int)):
            return float(value)
    if origin is list and isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def to_json(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name), depth + 1) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json(v, depth + 1) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def load_all() -> None:
    """Import every module that registers actions."""
    from abp_toolkit import (analyze, assets, cs, formatting, generate, lint, python_tools,  # noqa: F401
                             run, scripts)


def get(action_id: str) -> Action:
    load_all()
    a = ACTIONS.get(action_id)
    if a is None:
        close = [x for x in ACTIONS if x.split(".")[0] == action_id.split(".")[0]][:10]
        raise ToolkitError(f"no action {action_id!r}" + (f"; in that group: {', '.join(close)}" if close else ""))
    return a


def call(action_id: str, args: Optional[dict] = None, *, workspace: Optional[Path] = None) -> Any:
    """Run an action with JSON arguments. `workspace` is the folder file paths are relative to and confined to."""
    a = get(action_id)
    kwargs = bind(a.fn, dict(args or {}))
    if "workspace" in inspect.signature(a.fn).parameters:
        kwargs["workspace"] = Path(workspace or Path.cwd()).resolve()
    return to_json(a.fn(**kwargs))


def catalog(group_name: str = "") -> list[dict]:
    load_all()
    return [a.describe() for a in sorted(ACTIONS.values(), key=lambda a: a.id) if not group_name or a.group == group_name]
