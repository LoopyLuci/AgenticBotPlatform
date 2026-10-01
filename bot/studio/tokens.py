"""The pages' theme tokens (CSS custom properties: colours, fonts), read from a page and changed in a variant.

Each page defines them in three blocks at the top of its <style>: light (`:root {`), and dark twice (the explicit
`:root[data-theme="dark"] {` and the OS-preference one inside `@media (prefers-color-scheme: dark)`); a dark change
is made in both dark blocks, so they never drift apart. The dashboard and the desktop app are changed together."""
from __future__ import annotations

import re

from bot.studio import variants

PAGES = ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html")
_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;{}]+?)\s*;")
_NAME = re.compile(r"^--[\w-]{1,40}$")
_VALUE = re.compile(r"^[#\w\s,().%'\"+-/]{1,200}$")


def _blocks(css: str) -> dict[str, list[tuple[int, int]]]:
    """Where each mode's token blocks are: (start, end) of the text between their braces."""
    out: dict[str, list[tuple[int, int]]] = {"light": [], "dark": []}
    for m in re.finditer(r':root(\[data-theme="dark"\]|:not\(\[data-theme="light"\]\))?\s*\{', css):
        start = m.end()
        end = css.index("}", start)
        body = css[start:end]
        if "--" not in body:
            continue                      # a rule for one element, not a token block
        out["dark" if m.group(1) else "light"].append((start, end))
    return out


def read(vid: str | None = None, page: str = PAGES[0]) -> dict:
    html = variants.read(vid, page)
    head = html[:html.find("</style>") if "</style>" in html else len(html)]
    out: dict = {"light": {}, "dark": {}}
    for mode, spans in _blocks(head).items():
        for a, b in spans:
            for name, value in _DECL.findall(head[a:b]):
                out[mode].setdefault(name, value.strip())
    return out


def set_tokens(vid: str, mode: str, values: dict[str, str]) -> dict:
    """Change tokens in a variant (both pages). mode: light | dark. Only tokens that already exist can be changed;
    values are plain CSS values (colours, lengths, font lists)."""
    if mode not in ("light", "dark"):
        raise variants.StudioError("mode is light or dark")
    for n, v in values.items():
        if not _NAME.match(n) or not _VALUE.match(str(v)) or any(s in str(v) for s in ("url(", "expression", "@")):
            raise variants.StudioError(f"{n}: {v!r} is not a plain CSS value")
    changed: dict[str, list[str]] = {}
    for page in PAGES:
        html = variants.read(vid, page)
        cut = html.find("</style>")
        head, rest = html[:cut], html[cut:]
        spans = _blocks(head)[mode]
        for a, b in sorted(spans, reverse=True):          # from the end, so earlier offsets stay valid
            body = head[a:b]
            for n, v in values.items():
                body, k = re.subn(rf"({re.escape(n)}\s*:\s*)[^;{{}}]+?(\s*;)", lambda m, v=v: m.group(1) + str(v) + m.group(2),
                                  body)
                if k:
                    changed.setdefault(page, []).append(n)
            head = head[:a] + body + head[b:]
        unknown = [n for n in values if n not in changed.get(page, [])]
        if unknown and page == PAGES[0]:                # the dashboard defines them all; the desktop app follows
            raise variants.StudioError(f"no such {mode} token(s): {', '.join(unknown)}")
        if changed.get(page):
            variants.write(vid, page, head + rest, why="theme", source="person")
    return {"variant": vid, "mode": mode, "changed": {p: sorted(set(v)) for p, v in changed.items()}}
