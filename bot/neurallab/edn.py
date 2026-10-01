"""Enough EDN (Clojure's data notation) to read and write BrainBuilder's graph files (*.bbir.edn) and component
descriptors (components/*.edn): maps, vectors, lists, sets, keywords, strings, numbers, nil, true/false, comments.
Keywords read as plain strings without the colon (":graph-id" -> "graph-id"); write() turns map keys back into
keywords and leaves string values as strings.
"""
from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?M?N?$")
_DELIM = set(" \t\r\n,()[]{}\"';")


class EDNError(ValueError):
    pass


class _Reader:
    def __init__(self, text: str):
        self.s, self.i = text, 0

    def ws(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r\n,":
                self.i += 1
            elif c == ";":
                while self.i < len(s) and s[self.i] != "\n":
                    self.i += 1
            elif s.startswith("#_", self.i):               # discard the next form
                self.i += 2
                self.read()
            else:
                break

    def read(self) -> Any:
        self.ws()
        if self.i >= len(self.s):
            raise EDNError("unexpected end of input")
        c = self.s[self.i]
        if c == "{":
            self.i += 1
            items = self._seq("}")
            if len(items) % 2:
                raise EDNError("a map needs an even number of forms")
            return {_key(items[j]): items[j + 1] for j in range(0, len(items), 2)}
        if c == "[":
            self.i += 1
            return self._seq("]")
        if c == "(":
            self.i += 1
            return self._seq(")")
        if self.s.startswith("#{", self.i):
            self.i += 2
            return self._seq("}")
        if c == '"':
            return self._str()
        if c == "\\":                                       # a character literal
            self.i += 2
            start = self.i - 1
            while self.i < len(self.s) and self.s[self.i] not in _DELIM:
                self.i += 1
            tok = self.s[start:self.i]
            return {"newline": "\n", "space": " ", "tab": "\t"}.get(tok, tok[0])
        if c == "#":                                        # a tagged literal: #inst "...", #uuid "..."
            self.i += 1
            self._atom()
            return self.read()
        return self._atom_value()

    def _seq(self, end: str) -> list:
        out = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise EDNError(f"missing {end}")
            if self.s[self.i] == end:
                self.i += 1
                return out
            out.append(self.read())

    def _str(self) -> str:
        self.i += 1
        out = []
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                self.i += 1
                e = s[self.i]
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(e, e))
            else:
                out.append(c)
            self.i += 1
        raise EDNError("unterminated string")

    def _atom(self) -> str:
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in _DELIM:
            self.i += 1
        return self.s[start:self.i]

    def _atom_value(self):
        tok = self._atom()
        if not tok:
            raise EDNError(f"unexpected {self.s[self.i]!r} at {self.i}")
        if tok == "nil":
            return None
        if tok in ("true", "false"):
            return tok == "true"
        if tok.startswith(":"):
            return tok[1:]
        if _NUM.match(tok):
            t = tok.rstrip("MN")
            return float(t) if any(ch in t for ch in ".eE") else int(t)
        return tok                                          # a symbol


def _key(k):
    return k if isinstance(k, (str, int, float, bool)) or k is None else str(k)


def loads(text: str) -> Any:
    r = _Reader(text)
    v = r.read()
    r.ws()
    if r.i != len(r.s):
        raise EDNError(f"extra content after position {r.i}")
    return v


def _w(v: Any, key: bool = False) -> str:
    if v is None:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if key:
            return ":" + v
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_w(k, True)} {_w(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_w(x) for x in v) + "]"
    raise EDNError(f"can't write {type(v).__name__} as EDN")


def dumps(v: Any) -> str:
    return _w(v)
