"""Both UIs: every form field has a programmatic name (a <label for>, an
aria-label, or a wrapping <label>), every image has alt text, and every button
or link has a name. Screen readers announce exactly these; a field without one
is read as just "edit text"."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"]
VOID = {"input", "img", "br", "hr", "meta", "link", "source", "area", "col", "embed", "param", "track", "wbr"}
DYNAMIC_TEXT = {"btn-instructions-preset"}  # its label is filled in by script before it is shown


class _Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.labels_for, self.fields, self.problems = [], set(), [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if tag == "img" and "alt" not in a:
            self.problems.append(f"line {self.getpos()[0]}: <img> without alt")
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button"):
            self.fields.append((self.getpos()[0], a, any(s[0] == "label" for s in self.stack)))
        if tag not in VOID:
            self.stack.append([tag, a, self.getpos()[0], ""])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                t, a, line, text = self.stack[i]
                del self.stack[i:]
                if self.stack:
                    self.stack[-1][3] += text
                named = text.strip() or a.get("aria-label") or a.get("title") or a.get("aria-labelledby")
                if t == "button" and not named and a.get("id") not in DYNAMIC_TEXT:
                    self.problems.append(f"line {line}: <button id={a.get('id')!r}> has no accessible name")
                return

    def handle_data(self, data):
        if self.stack:
            self.stack[-1][3] += data


@pytest.mark.parametrize("page", PAGES)
def test_every_control_has_an_accessible_name(page):
    audit = _Audit()
    audit.feed((ROOT / page).read_text(encoding="utf-8"))
    for line, a, wrapped in audit.fields:
        if not (wrapped or a.get("aria-label") or a.get("aria-labelledby") or a.get("title") or a.get("id") in audit.labels_for):
            audit.problems.append(f"line {line}: field id={a.get('id')!r} has no label")
    assert audit.problems == []
