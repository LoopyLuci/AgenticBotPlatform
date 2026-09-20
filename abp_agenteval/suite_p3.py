"""Seed tasks for orientation and search (repo map, code search)."""
from __future__ import annotations

from . import graders as g
from .task import Call, Say, Task


def p3_suite() -> list[Task]:
    return [
        Task(
            id="search_by_concept", category="search", title="Find code by describing it, not by exact text",
            prompt="Which file handles retrying when refreshing an auth token fails? Reply with just the file path.",
            files={"auth/session.py": "def refresh_token(client):\n    # retry with backoff when the token refresh fails\n    return client.renew()\n",
                   "ui/button.py": "def draw_button(label):\n    return label\n",
                   "billing/invoice.py": "def total(items):\n    return sum(items)\n"},
            script=[Call("code_search", {"query": "retry token refresh"}), Say("auth/session.py")],
            graders=[g.finished_ok(), g.reply_matches(r"auth/session\.py"), g.used_tool("code_search"), g.within_iterations(3)],
        ),
        Task(
            id="orient_with_repo_map", category="search", title="Get the shape of a project before reading files",
            prompt="Which class is used by the most other files in this project? Reply with just its name.",
            files={"core.py": "class CoreEngine:\n    def run(self):\n        pass\n",
                   "a.py": "from core import CoreEngine\n\ndef use_a():\n    return CoreEngine()\n",
                   "b.py": "from core import CoreEngine\n\ndef use_b():\n    return CoreEngine()\n",
                   "c.py": "class Lonely:\n    pass\n"},
            script=[Call("repo_map", {}), Say("CoreEngine")],
            graders=[g.finished_ok(), g.reply_matches(r"CoreEngine"), g.used_tool("repo_map"), g.did_not_use_tool("read_file")],
        ),
    ]
