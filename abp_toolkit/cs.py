"""Computer-science utilities an agent reaches for while programming: regular expressions, encodings, hashes, numbers
and bits, text diffs, JSON Schema, JSON queries, dates, SQL on data, graphs, and measuring how code scales.
"""
from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.parse
import uuid
import zlib
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, read_text

group("cs", "Computer science: regex, encodings, hashes, numbers & bits, diffs, JSON Schema/queries, dates, SQL, graphs, complexity")


@action("cs.regex")
def regex(pattern: str, text: str, flags: str = "", replace: Optional[str] = None, max_matches: int = 200) -> dict:
    """Test a regular expression: every match with its groups and positions, or the text after a replacement

    pattern: a Python regular expression
    text: the text to search
    flags: any of i (ignore case), m (multiline), s (dot matches newline), x (verbose), a (ASCII)
    replace: if given, the replacement (\\1 or \\g<name> refer to groups)
    """
    f = 0
    for c in flags.lower():
        f |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}.get(c, 0)
    try:
        rx = re.compile(pattern, f)
    except re.error as e:
        raise ToolkitError(f"invalid pattern at position {e.pos}: {e.msg}") from e
    matches = []
    for m in rx.finditer(text):
        matches.append({"match": m.group(0), "start": m.start(), "end": m.end(), "groups": list(m.groups()),
                        "named": m.groupdict(), "line": text.count("\n", 0, m.start()) + 1})
        if len(matches) >= max_matches:
            break
    out: dict = {"count": len(matches), "matches": matches, "group_count": rx.groups, "group_names": list(rx.groupindex)}
    if replace is not None:
        out["replaced"], out["replacements"] = rx.subn(replace, text)
    return out


ENCODINGS = ("base64", "base64url", "base32", "hex", "url", "url_component", "html", "rot13", "unicode_escape",
             "punycode", "quoted_printable", "binary", "morse")
MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....", "I": "..",
         "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
         "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
         "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....", "7": "--...", "8": "---..",
         "9": "----.", " ": "/"}


@action("cs.encode")
def encode(text: str, encoding: str, decode: bool = False) -> dict:
    """Encode or decode text: base64, base64url, base32, hex, url, html, rot13, unicode_escape, punycode, quoted_printable, binary, morse

    text: the input
    encoding: which encoding
    decode: decode instead of encode
    """
    import codecs
    import html
    import quopri
    e = encoding.lower()
    try:
        if e == "base64":
            out = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace") if decode else base64.b64encode(text.encode()).decode()
        elif e == "base64url":
            out = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace") if decode else base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        elif e == "base32":
            out = base64.b32decode(text + "=" * (-len(text) % 8)).decode("utf-8", "replace") if decode else base64.b32encode(text.encode()).decode()
        elif e == "hex":
            out = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", text)).decode("utf-8", "replace") if decode else text.encode().hex()
        elif e == "url":
            out = urllib.parse.unquote_plus(text) if decode else urllib.parse.quote_plus(text)
        elif e == "url_component":
            out = urllib.parse.unquote(text) if decode else urllib.parse.quote(text, safe="")
        elif e == "html":
            out = html.unescape(text) if decode else html.escape(text)
        elif e == "rot13":
            out = codecs.encode(text, "rot13")
        elif e == "unicode_escape":
            out = text.encode("latin-1", "backslashreplace").decode("unicode_escape") if decode else text.encode("unicode_escape").decode("ascii")
        elif e == "punycode":
            out = text.encode("ascii").decode("idna") if decode else text.encode("idna").decode("ascii")
        elif e == "quoted_printable":
            out = quopri.decodestring(text.encode()).decode("utf-8", "replace") if decode else quopri.encodestring(text.encode()).decode()
        elif e == "binary":
            out = bytes(int(b, 2) for b in text.split()).decode("utf-8", "replace") if decode else " ".join(f"{b:08b}" for b in text.encode())
        elif e == "morse":
            rev = {v: k for k, v in MORSE.items()}
            out = "".join(rev.get(t, "?") for t in text.split(" ")) if decode else " ".join(MORSE.get(c, "?") for c in text.upper())
        else:
            raise ToolkitError(f"encoding is one of {', '.join(ENCODINGS)}")
    except (ValueError, binascii.Error, UnicodeError) as err:
        raise ToolkitError(f"cannot {'decode' if decode else 'encode'} as {e}: {err}") from err
    return {"encoding": e, "decoded" if decode else "encoded": out}


@action("cs.hash")
def hash_text(workspace: Path, text: str = "", path: str = "", algorithms: Optional[list[str]] = None, hmac_key: str = "") -> dict:
    """Hashes and checksums of text or a file: md5, sha1, sha256, sha512, sha3_256, blake2b, crc32, adler32 (and HMAC)

    text: the text to hash (or give path)
    path: a file inside the working folder
    algorithms: which ones (default: md5, sha1, sha256, sha512, crc32)
    hmac_key: if given, HMAC with this key instead of plain hashes
    """
    if path:
        data = inside(workspace, path, must_exist=True).read_bytes()
    else:
        data = text.encode("utf-8")
    algs = algorithms or ["md5", "sha1", "sha256", "sha512", "crc32"]
    out = {}
    for a in algs:
        if a == "crc32":
            out[a] = f"{zlib.crc32(data) & 0xffffffff:08x}"
        elif a == "adler32":
            out[a] = f"{zlib.adler32(data) & 0xffffffff:08x}"
        elif hmac_key:
            out[f"hmac-{a}"] = hmac.new(hmac_key.encode(), data, a).hexdigest()
        else:
            try:
                out[a] = hashlib.new(a, data).hexdigest()
            except ValueError:
                raise ToolkitError(f"unknown algorithm {a!r}; available: {', '.join(sorted(hashlib.algorithms_available))}") from None
    return {"bytes": len(data), "hashes": out}


@action("cs.number")
def number(value: str, from_base: int = 0) -> dict:
    """A number in every base and bit view: decimal, hex, octal, binary, two's complement, float bits, bit counts

    value: the number (123, 0xff, 0b1010, 0o17, -5, 3.14, 1e9)
    from_base: read value in this base (0 = from its prefix)
    """
    v = value.strip().replace("_", "")
    try:
        n: Any = int(v, from_base) if from_base or re.match(r"^-?(0[xob])?[0-9a-fA-F]+$", v) and not re.match(r"^-?\d*\.\d", v) else float(v)
    except ValueError:
        try:
            n = float(v)
        except ValueError:
            raise ToolkitError(f"not a number: {value!r}") from None
    import struct
    if isinstance(n, float) and n.is_integer() and "e" not in v.lower() and "." not in v:
        n = int(n)
    if isinstance(n, float):
        bits64 = struct.unpack(">Q", struct.pack(">d", n))[0]
        bits32 = struct.unpack(">I", struct.pack(">f", n))[0]
        return {"float": n, "hex": n.hex(), "ieee754_double": f"{bits64:064b}", "ieee754_single": f"{bits32:032b}",
                "as_fraction": "%d/%d" % n.as_integer_ratio()}
    out = {"decimal": n, "hex": hex(n), "octal": oct(n), "binary": bin(n), "bit_length": n.bit_length(),
           "popcount": bin(n).count("1") if n >= 0 else None, "bytes_big_endian": None}
    for width in (8, 16, 32, 64):
        if -(1 << (width - 1)) <= n < (1 << width):
            u = n & ((1 << width) - 1)
            out[f"u{width}"] = {"hex": f"0x{u:0{width // 4}x}", "binary": f"{u:0{width}b}",
                                "signed": u - (1 << width) if u >> (width - 1) else u, "unsigned": u}
    if n >= 0:
        out["bytes_big_endian"] = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big").hex(" ")
        out["is_power_of_two"] = n > 0 and n & (n - 1) == 0
        if n < 10**12:
            out["prime"] = n > 1 and all(n % p for p in range(2, int(math.isqrt(n)) + 1))
    return out


@action("cs.diff")
def diff(a: str, b: str, context: int = 3, by: str = "line") -> dict:
    """Differences between two texts: a unified diff, a similarity ratio and the changed lines (or words)

    a: the old text
    b: the new text
    by: line or word
    """
    if by == "word":
        sa, sb = re.findall(r"\S+|\s+", a), re.findall(r"\S+|\s+", b)
        sm = difflib.SequenceMatcher(None, sa, sb)
        ops = [{"op": tag, "old": "".join(sa[i1:i2]), "new": "".join(sb[j1:j2])} for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
        return {"similarity": round(sm.ratio(), 4), "changes": ops[:500]}
    la, lb = a.splitlines(keepends=True), b.splitlines(keepends=True)
    udiff = "".join(difflib.unified_diff(la, lb, "a", "b", n=context))
    sm = difflib.SequenceMatcher(None, la, lb)
    return {"similarity": round(sm.ratio(), 4), "added": sum(j2 - j1 for t, i1, i2, j1, j2 in sm.get_opcodes() if t in ("insert", "replace")),
            "removed": sum(i2 - i1 for t, i1, i2, j1, j2 in sm.get_opcodes() if t in ("delete", "replace")), "unified": udiff[:100_000]}


@action("cs.json_schema")
def json_schema(document: Any, schema: Optional[dict] = None, infer: bool = False) -> dict:
    """Validate a JSON document against a JSON Schema (every error with its path), or infer a schema from the document

    document: the JSON value (or a JSON string)
    schema: the schema to validate against
    infer: build a schema that describes the document instead
    """
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            pass
    if infer or schema is None:
        return {"schema": _infer(document)}
    try:
        import jsonschema
    except ImportError:
        raise ToolkitError("validation needs the jsonschema package (pip install jsonschema)") from None
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    errors = sorted(cls(schema).iter_errors(document), key=lambda e: list(e.path))
    return {"valid": not errors, "errors": [{"path": "/" + "/".join(map(str, e.path)), "message": e.message,
                                             "rule": e.validator} for e in errors[:200]]}


def _infer(v: Any) -> dict:
    if isinstance(v, bool):
        return {"type": "boolean"}
    if isinstance(v, int):
        return {"type": "integer"}
    if isinstance(v, float):
        return {"type": "number"}
    if isinstance(v, str):
        s: dict = {"type": "string"}
        if re.match(r"^\d{4}-\d{2}-\d{2}T", v):
            s["format"] = "date-time"
        elif re.match(r"^[^@\s]+@[^@\s]+\.\w+$", v):
            s["format"] = "email"
        elif re.match(r"^https?://", v):
            s["format"] = "uri"
        return s
    if v is None:
        return {"type": "null"}
    if isinstance(v, list):
        items = [_infer(x) for x in v]
        uniq = [json.loads(x) for x in dict.fromkeys(json.dumps(i, sort_keys=True) for i in items)]
        return {"type": "array", "items": uniq[0] if len(uniq) == 1 else {"anyOf": uniq} if uniq else {}}
    if isinstance(v, dict):
        return {"type": "object", "properties": {k: _infer(x) for k, x in v.items()}, "required": sorted(v)}
    return {}


@action("cs.json_query")
def json_query(document: Any, path: str) -> dict:
    """Pick values out of JSON with a simple path: a.b[0].c, items[*].name, $..id (every id at any depth)

    document: the JSON value (or a JSON string)
    path: dotted path; [n] indexes, [*] every element, .. searches every depth
    """
    if isinstance(document, str):
        document = json.loads(document)
    raw_tokens = re.findall(r"\.\.|\.?[^.\[\]]+|\[(?:\*|-?\d+)\]", path.lstrip("$"))
    current = [document]
    deep = False
    for tok in raw_tokens:
        if tok == "..":
            deep = True
            continue
        nxt = []
        for c in current:
            pool = list(_descend(c)) if deep else [c]
            for node in pool:
                if tok.startswith("["):
                    inner = tok[1:-1]
                    if isinstance(node, list):
                        nxt.extend(node if inner == "*" else [node[int(inner)]] if -len(node) <= int(inner) < len(node) else [])
                else:
                    key = tok.lstrip(".")
                    if isinstance(node, dict) and key in node:
                        nxt.append(node[key])
                    elif key == "*" and isinstance(node, dict):
                        nxt.extend(node.values())
        current, deep = nxt, False
    return {"count": len(current), "values": current[:1000]}


def _descend(v: Any):
    yield v
    if isinstance(v, dict):
        for x in v.values():
            yield from _descend(x)
    elif isinstance(v, list):
        for x in v:
            yield from _descend(x)


@action("cs.time")
def time_convert(value: str = "now", to_timezone: str = "UTC", add: str = "") -> dict:
    """Dates and times: parse (ISO 8601, Unix seconds or milliseconds, now), convert time zones, add durations

    value: a date/time, a Unix timestamp, or now
    to_timezone: an IANA zone (Europe/Paris, America/New_York) or UTC
    add: a duration to add, like 3d4h, -90m, 2w
    """
    from zoneinfo import ZoneInfo
    v = value.strip()
    if v.lower() == "now":
        dt = datetime.now(timezone.utc)
    elif re.match(r"^-?\d{9,13}(\.\d+)?$", v):
        n = float(v)
        dt = datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ToolkitError(f"cannot read {value!r} as a date (use ISO 8601 or a Unix timestamp)") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    if add:
        total = timedelta()
        for num, unit in re.findall(r"(-?\d+(?:\.\d+)?)\s*(w|d|h|m|s|ms)", add):
            total += timedelta(**{{"w": "weeks", "d": "days", "h": "hours", "m": "minutes", "s": "seconds",
                                   "ms": "milliseconds"}[unit]: float(num)})
        dt += total
    try:
        tz = ZoneInfo(to_timezone) if to_timezone.upper() != "UTC" else timezone.utc
    except Exception:  # noqa: BLE001
        raise ToolkitError(f"unknown time zone {to_timezone!r} (on Windows, pip install tzdata)") from None
    local = dt.astimezone(tz)
    return {"iso": local.isoformat(), "utc": dt.astimezone(timezone.utc).isoformat(), "unix": int(dt.timestamp()),
            "unix_ms": int(dt.timestamp() * 1000), "weekday": local.strftime("%A"), "iso_week": local.isocalendar()[1],
            "day_of_year": local.timetuple().tm_yday, "rfc2822": local.strftime("%a, %d %b %Y %H:%M:%S %z")}


@action("cs.sql")
def sql(workspace: Path, query: str, csv_files: Optional[list[str]] = None, json_rows: Optional[dict] = None,
        database: str = "", max_rows: int = 500) -> dict:
    """Run SQL (SQLite) over CSV files, JSON rows or a SQLite database file, and return the rows

    query: the SQL
    csv_files: CSV files inside the working folder, each loaded as a table named after the file
    json_rows: {table: [{column: value}, ...]} loaded as tables
    database: a SQLite file inside the working folder (opened read-only)
    """
    import csv
    if database:
        dbp = inside(workspace, database, must_exist=True)
        con = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(":memory:")
    for f in csv_files or []:
        p = inside(workspace, f, must_exist=True)
        rows = list(csv.reader(read_text(p).splitlines()))
        if not rows:
            continue
        table = re.sub(r"\W", "_", p.stem)
        cols = [re.sub(r"\W", "_", c) or f"c{i}" for i, c in enumerate(rows[0])]
        con.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in cols)})')
        con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})', [r + [None] * (len(cols) - len(r)) for r in rows[1:]])
    for table, rows in (json_rows or {}).items():
        if not rows:
            continue
        cols = sorted({k for r in rows for k in r})
        con.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
        con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
                        [[json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else r.get(c) for c in cols] for r in rows])
    try:
        cur = con.execute(query)
    except sqlite3.Error as e:
        raise ToolkitError(f"SQL error: {e}") from e
    columns = [d[0] for d in cur.description or []]
    rows = cur.fetchmany(max_rows + 1)
    return {"columns": columns, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}


@action("cs.graph")
def graph(edges: list, algorithm: str, source: str = "", target: str = "", directed: bool = True) -> dict:
    """Graph algorithms on your data: shortest path (weighted), topological order, cycles, connected components, BFS order

    edges: [[from, to], ...] or [[from, to, weight], ...]
    algorithm: shortest_path, topological_sort, cycles, components, bfs, degrees
    source: the start node (shortest_path, bfs)
    target: the end node (shortest_path)
    directed: whether edges go one way
    """
    import heapq
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in edges:
        a, b = str(e[0]), str(e[1])
        w = float(e[2]) if len(e) > 2 else 1.0
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, [])
        if not directed:
            adj[b].append((a, w))
    if algorithm == "shortest_path":
        if source not in adj or target not in adj:
            raise ToolkitError("source and target must be nodes of the graph")
        dist, prev, heap = {source: 0.0}, {}, [(0.0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == target:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, w in adj[u]:
                if w < 0:
                    raise ToolkitError("negative weights are not supported")
                if d + w < dist.get(v, math.inf):
                    dist[v], prev[v] = d + w, u
                    heapq.heappush(heap, (d + w, v))
        if target not in dist:
            return {"reachable": False}
        path, n = [target], target
        while n != source:
            n = prev[n]
            path.append(n)
        return {"reachable": True, "distance": dist[target], "path": path[::-1]}
    if algorithm == "topological_sort":
        indeg = {n: 0 for n in adj}
        for u in adj:
            for v, _ in adj[u]:
                indeg[v] += 1
        q = deque(sorted(n for n, d in indeg.items() if d == 0))
        order = []
        while q:
            u = q.popleft()
            order.append(u)
            for v, _ in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        return {"order": order, "has_cycle": len(order) != len(adj)}
    if algorithm == "cycles":
        cycles, color, stack = [], {}, []

        def dfs(u):
            color[u] = 1
            stack.append(u)
            for v, _ in adj[u]:
                if color.get(v) == 1 and len(cycles) < 50:
                    cycles.append(stack[stack.index(v):] + [v])
                elif not color.get(v):
                    dfs(v)
            stack.pop()
            color[u] = 2
        import sys
        sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
        for n in adj:
            if not color.get(n):
                dfs(n)
        return {"cycles": cycles}
    if algorithm == "components":
        und: dict[str, set] = {n: set() for n in adj}
        for u in adj:
            for v, _ in adj[u]:
                und[u].add(v)
                und[v].add(u)
        seen, comps = set(), []
        for n in und:
            if n in seen:
                continue
            comp, q = [], deque([n])
            seen.add(n)
            while q:
                u = q.popleft()
                comp.append(u)
                for v in und[u] - seen:
                    seen.add(v)
                    q.append(v)
            comps.append(sorted(comp))
        return {"count": len(comps), "components": sorted(comps, key=len, reverse=True)}
    if algorithm == "bfs":
        if source not in adj:
            raise ToolkitError("source must be a node of the graph")
        order, depth, q = [], {source: 0}, deque([source])
        while q:
            u = q.popleft()
            order.append({"node": u, "depth": depth[u]})
            for v, _ in adj[u]:
                if v not in depth:
                    depth[v] = depth[u] + 1
                    q.append(v)
        return {"order": order}
    if algorithm == "degrees":
        indeg: dict[str, int] = {n: 0 for n in adj}
        for u in adj:
            for v, _ in adj[u]:
                indeg[v] += 1
        return {"nodes": len(adj), "edges": sum(len(v) for v in adj.values()),
                "degrees": {n: {"out": len(adj[n]), "in": indeg[n]} for n in sorted(adj)}}
    raise ToolkitError("algorithm is shortest_path, topological_sort, cycles, components, bfs or degrees")


@action("cs.big_o", executes=True)
def big_o(function_code: str, input_code: str, sizes: Optional[list[int]] = None, timeout_s: float = 120) -> dict:
    """Measure how a Python function's time grows with input size and name the best-fitting complexity (O(1)..O(2^n))

    function_code: Python defining a function named f
    input_code: a Python expression building the input from n, e.g. list(range(n)) or random.sample(range(n*10), n)
    sizes: the input sizes to try (default 100 to 102400)
    """
    import subprocess
    import sys
    sizes = sizes or [100, 400, 1600, 6400, 25600, 102400]
    script = (function_code + "\nimport time, random, json, sys\nres=[]\nfor n in " + json.dumps(sizes) + ":\n"
              "    data = " + input_code + "\n    reps = 1\n    while True:\n        t=time.perf_counter()\n"
              "        for _ in range(reps): f(data)\n        dt=time.perf_counter()-t\n"
              "        if dt > 0.05 or reps > 10000: break\n        reps *= 4\n"
              "    res.append([n, dt/reps])\n    if dt/reps > 3: break\nprint(json.dumps(res))\n")
    try:
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout_s,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired:
        raise ToolkitError(f"did not finish within {timeout_s}s (try smaller sizes)") from None
    if p.returncode != 0:
        raise ToolkitError(p.stderr.strip()[-1500:])
    points = json.loads(p.stdout.strip().splitlines()[-1])
    models = {"O(1)": lambda n: 1, "O(log n)": lambda n: math.log2(n), "O(n)": lambda n: n,
              "O(n log n)": lambda n: n * math.log2(n), "O(n^2)": lambda n: n * n, "O(n^3)": lambda n: n ** 3,
              "O(2^n)": lambda n: 2.0 ** min(n, 1000)}
    fits = {}
    for name, g in models.items():
        xs = [g(n) for n, _ in points]
        ys = [t for _, t in points]
        c = sum(x * y for x, y in zip(xs, ys)) / (sum(x * x for x in xs) or 1)
        rel_err = sum(((c * x - y) / y) ** 2 for x, y in zip(xs, ys) if y > 0) / len(xs)
        fits[name] = rel_err
    best = min(fits, key=fits.get)
    return {"best_fit": best, "measurements": [{"n": n, "seconds": t} for n, t in points],
            "fit_error": {k: round(v, 4) for k, v in sorted(fits.items(), key=lambda kv: kv[1])}}


@action("cs.generate")
def generate(kind: str, count: int = 1, length: int = 24) -> dict:
    """Identifiers and random values: uuid4, uuid7-like time-ordered ids, ulid, nanoid, secure tokens, passwords, hex

    kind: uuid4, uuid1, time_ordered, ulid, nanoid, token, password, hex, number
    count: how many
    length: length for nanoid, token, password and hex
    """
    count = max(1, min(count, 1000))
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def one() -> str:
        if kind == "uuid4":
            return str(uuid.uuid4())
        if kind == "uuid1":
            return str(uuid.uuid1())
        if kind == "time_ordered":
            ms = int(time.time() * 1000)
            rand = secrets.randbits(74)
            v = (ms << 80) | (0x7 << 76) | ((rand >> 62) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
            return str(uuid.UUID(int=v))
        if kind == "ulid":
            v = (int(time.time() * 1000) << 80) | secrets.randbits(80)
            return "".join(alphabet[(v >> (5 * i)) & 31] for i in range(25, -1, -1))
        if kind == "nanoid":
            chars = "_-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            return "".join(secrets.choice(chars) for _ in range(length))
        if kind == "token":
            return secrets.token_urlsafe(length)[:length]
        if kind == "password":
            pools = ["abcdefghijkmnopqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "!@#$%^&*-_=+?"]
            chars = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(max(0, length - 4))]
            secrets.SystemRandom().shuffle(chars)
            return "".join(chars)
        if kind == "hex":
            return secrets.token_hex((length + 1) // 2)[:length]
        if kind == "number":
            return str(secrets.randbelow(10 ** length))
        raise ToolkitError("kind is uuid4, uuid1, time_ordered, ulid, nanoid, token, password, hex or number")
    return {"kind": kind, "values": [one() for _ in range(count)]}


@action("cs.text")
def text_stats(text: str) -> dict:
    """What a piece of text contains: length in characters, bytes and tokens (estimate), lines, words, unusual characters

    text: the text
    """
    odd = {}
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("C") and ch not in "\n\r\t" or cat in ("Zs",) and ch != " " or ch in "\u200b\u200c\u200d\ufeff\u2028\u2029":
            odd[f"U+{ord(ch):04X}"] = unicodedata.name(ch, "control")
    words = re.findall(r"\w+", text)
    return {"characters": len(text), "bytes_utf8": len(text.encode("utf-8")), "lines": text.count("\n") + (1 if text else 0),
            "words": len(words), "unique_words": len({w.lower() for w in words}), "tokens_estimate": max(1, round(len(text) / 4)),
            "non_ascii": sum(1 for c in text if ord(c) > 127), "invisible_or_control": odd,
            "line_endings": {"crlf": text.count("\r\n"), "lf": text.count("\n") - text.count("\r\n"), "cr": text.count("\r") - text.count("\r\n")}}


@action("cs.cron")
def cron(expression: str, count: int = 5, start: str = "") -> dict:
    """Explain a cron expression (minute hour day month weekday) and list its next run times

    expression: e.g. */15 9-17 * * 1-5
    count: how many upcoming times
    start: start from this ISO time (default now, UTC)
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ToolkitError("a cron expression has 5 fields: minute hour day-of-month month day-of-week")
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    sets = []
    for f, (lo, hi) in zip(fields, ranges):
        vals: set[int] = set()
        for part in f.split(","):
            step = 1
            if "/" in part:
                part, s = part.split("/")
                step = int(s)
            if part == "*":
                a, b = lo, hi
            elif "-" in part:
                a, b = map(int, part.split("-"))
            else:
                a = b = int(part)
            vals.update(range(a, b + 1, step))
        sets.append({v % 7 for v in vals} if (lo, hi) == (0, 6) else vals)   # weekday 7 is Sunday, like 0
    t = datetime.fromisoformat(start) if start else datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t += timedelta(minutes=1)
    out = []
    for _ in range(600000):
        dom_any, dow_any = fields[2] == "*", fields[4] == "*"
        day_ok = (t.day in sets[2]) if dow_any else (t.isoweekday() % 7 in sets[4]) if dom_any else (t.day in sets[2] or t.isoweekday() % 7 in sets[4])
        if t.minute in sets[0] and t.hour in sets[1] and t.month in sets[3] and day_ok:
            out.append(t.isoformat())
            if len(out) >= count:
                break
        t += timedelta(minutes=1)
    return {"expression": expression, "next": out}
