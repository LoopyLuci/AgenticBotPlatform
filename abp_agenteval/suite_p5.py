"""Seed tasks for developer surfaces (roadmap P5): the agent sees a language server's verdict after an edit."""
from __future__ import annotations

import sys
from pathlib import Path

from . import graders as g
from .task import Call, Say, Task

_FAKE_SERVER = str(Path(__file__).resolve().parent.parent / "tests" / "fake_lsp_server.py")


def p5_suite() -> list[Task]:
    return [
        Task(
            id="fix_what_the_language_server_reports", category="code-intel",
            title="Repair a problem the language server reports right after an edit",
            prompt="Change the greeting in app.py to say hello. The language server checks each edit; leave no problems behind.",
            files={"app.py": "def greet():\n    return 'hi'\n"},
            config={"code_intel": {"lsp": {"enabled": True, "wait_s": 10,
                                           "servers": {"fake": {"command": [sys.executable, _FAKE_SERVER], "extensions": [".py"]}}}}},
            script=[Call("read_file", {"path": "app.py"}),
                    Call("edit_file", {"path": "app.py", "old_string": "'hi'", "new_string": "'hello' BROKEN"}),
                    Call("edit_file", {"path": "app.py", "old_string": "'hello' BROKEN", "new_string": "'hello'"}),
                    Say("Changed the greeting; the language server reports no problems.")],
            graders=[g.finished_ok(), g.file_equals("app.py", "def greet():\n    return 'hello'\n"), g.used_tool("edit_file"),
                     g.within_iterations(6)],
        ),
    ]
