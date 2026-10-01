"""The edits models answer with, applied exactly or not at all.

    FILE: bot/dashboard/static/vision-panel.js
    <<<<<<< SEARCH
    (text that is in the file now, exactly, once)
    =======
    (what replaces it)
    >>>>>>> REPLACE

    NEW FILE: bot/dashboard/static/my-widget.js
    ```js
    (the whole new file)
    ```

    EXPLANATION: one or two sentences

A block whose SEARCH text is not in the file exactly once is refused (with the reason), never guessed at."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FILE = re.compile(r"^FILE:\s*(\S+)\s*$", re.M)
_BLOCK = re.compile(r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n?>>>>>>> REPLACE", re.S)
_NEW = re.compile(r"^NEW FILE:\s*(\S+)\s*\r?\n```[\w+-]*\r?\n(.*?)\r?\n```", re.M | re.S)
_EXPLAIN = re.compile(r"^EXPLANATION:\s*(.+)$", re.M)


@dataclass
class Parsed:
    edits: list[tuple[str, str, str]] = field(default_factory=list)      # (file, search, replace)
    new_files: dict[str, str] = field(default_factory=dict)
    explanation: str = ""


def parse(text: str) -> Parsed:
    out = Parsed()
    m = _EXPLAIN.search(text)
    out.explanation = m.group(1).strip() if m else ""
    for nf in _NEW.finditer(text):
        out.new_files[nf.group(1)] = nf.group(2) + "\n"
    heads = list(_FILE.finditer(text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        for b in _BLOCK.finditer(text, h.end(), end):
            out.edits.append((h.group(1), b.group(1), b.group(2)))
    return out


def apply(current: str, search: str, replace: str) -> tuple[str, str]:
    """(new text, "") or (current, the reason it was refused)."""
    n = current.count(search)
    if not search.strip():
        return current, "an empty SEARCH"
    if n == 0:
        # a model often gets only line endings or trailing spaces wrong: match whole lines with those ignored (still
        # exactly once), and replace just those lines; every other line of the file stays byte for byte
        eol = "\r\n" if "\r\n" in current else "\n"
        lines = current.split(eol)
        want = [s.rstrip() for s in search.replace("\r\n", "\n").split("\n")]
        k = len(want)
        hits = [i for i in range(len(lines) - k + 1) if [x.rstrip() for x in lines[i:i + k]] == want]
        if len(hits) == 1:
            i = hits[0]
            new = lines[:i] + replace.replace("\r\n", "\n").split("\n") + lines[i + k:]
            return eol.join(new), ""
        return current, ("its SEARCH text is not in the file" if not hits else
                         f"its SEARCH text is in the file {len(hits)} times (give more of it)")
    if n > 1:
        return current, f"its SEARCH text is in the file {n} times (give more of it)"
    return current.replace(search, replace, 1), ""
