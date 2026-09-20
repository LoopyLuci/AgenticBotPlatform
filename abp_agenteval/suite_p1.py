"""Seed tasks for the P1 toolset (editing, search, planning, background jobs).
Each has a golden trajectory for scripted mode."""
from __future__ import annotations

import sys

from . import graders as g
from .task import Call, Say, Task

_PY = f'"{sys.executable}"'

_SETTINGS = "# settings\nDEBUG = True\nPORT = 8080\nNAME = 'demo'\n"

_LIB = "def compute_total(items):\n    return sum(items)\n"
_APP = "from lib import compute_total\n\nprint(compute_total([1, 2, 3]))\n"
_TEST = ("from lib import calculate_total\nfrom app import *\n"
         "assert calculate_total([1, 2, 3]) == 6\nprint('ok')\n")

_PATCH = """--- a/greeting.txt
+++ b/greeting.txt
@@ -1,3 +1,3 @@
 Hello,
-World
+ABP
 Goodbye
"""

_JOB = "import time\nprint('server ready', flush=True)\ntime.sleep(60)\n"


def p1_suite() -> list[Task]:
    return [
        Task(
            id="edit_in_place", category="editing", title="Change one setting without touching the rest",
            prompt="In settings.py set DEBUG to False. Change nothing else.",
            files={"settings.py": _SETTINGS},
            script=[Call("read_file", {"path": "settings.py"}),
                    Call("edit_file", {"path": "settings.py", "old_string": "DEBUG = True", "new_string": "DEBUG = False"}),
                    Say("DEBUG is now False.")],
            graders=[g.finished_ok(), g.file_equals("settings.py", "# settings\nDEBUG = False\nPORT = 8080\nNAME = 'demo'\n"),
                     g.used_tool("edit_file"), g.did_not_use_tool("write_file"), g.within_iterations(4)],
        ),
        Task(
            id="rename_across_files", category="editing", title="Find every use of a function and rename it",
            prompt="Rename compute_total to calculate_total everywhere, then run `python test_lib.py`.",
            files={"lib.py": _LIB, "app.py": _APP, "test_lib.py": _TEST.replace("from app import *\n", "")},
            script=[Call("grep", {"pattern": "compute_total", "output_mode": "files"}),
                    Call("read_file", {"path": "lib.py"}, more=(("read_file", {"path": "app.py"}),)),
                    Call("edit_file", {"path": "lib.py", "old_string": "def compute_total", "new_string": "def calculate_total"}),
                    Call("multi_edit", {"path": "app.py", "edits": [
                        {"old_string": "import compute_total", "new_string": "import calculate_total"},
                        {"old_string": "print(compute_total", "new_string": "print(calculate_total"}]}),
                    Call("run_shell", {"command": f"{_PY} test_lib.py"}),
                    Say("Renamed in lib.py and app.py; the test prints ok.")],
            graders=[g.finished_ok(), g.file_contains("lib.py", "def calculate_total"), g.file_contains("app.py", "calculate_total"),
                     g.command_passes(["python", "test_lib.py"], label="the test passes afterwards"),
                     g.used_tool("grep"), g.used_tool("edit_file")],
        ),
        Task(
            id="apply_a_patch", category="editing", title="Apply a unified diff",
            prompt="Apply this patch to greeting.txt:\n\n" + _PATCH,
            files={"greeting.txt": "Hello,\nWorld\nGoodbye\n"},
            script=[Call("read_file", {"path": "greeting.txt"}), Call("apply_patch", {"patch": _PATCH}), Say("Patched.")],
            graders=[g.finished_ok(), g.file_equals("greeting.txt", "Hello,\nABP\nGoodbye\n"), g.used_tool("apply_patch")],
        ),
        Task(
            id="stale_read_is_caught", category="safety", title="Read before editing, and recover from the refusal",
            prompt="In notes.txt change 'draft' to 'final'.",
            files={"notes.txt": "status: draft\n"},
            script=[Call("edit_file", {"path": "notes.txt", "old_string": "draft", "new_string": "final"}),
                    Call("read_file", {"path": "notes.txt"}),
                    Call("edit_file", {"path": "notes.txt", "old_string": "draft", "new_string": "final"}),
                    Say("Changed to final.")],
            graders=[g.finished_ok(), g.tool_status("edit_file", "failed"), g.file_equals("notes.txt", "status: final\n")],
        ),
        Task(
            id="grep_and_count", category="search", title="Count matches across a tree",
            prompt="How many TODO comments are in this project? Reply with just the number.",
            files={"a.py": "# TODO: one\nx = 1\n", "pkg/b.py": "# TODO: two\n# TODO: three\n", "pkg/c.py": "print('done')\n",
                   "node_modules/x.js": "// TODO: ignore me\n"},
            script=[Call("grep", {"pattern": "TODO", "output_mode": "count"}), Say("3")],
            graders=[g.finished_ok(), g.reply_matches(r"\b3\b"), g.used_tool("grep"), g.within_iterations(3)],
        ),
        Task(
            id="glob_and_answer", category="search", title="Find files by pattern",
            prompt="How many Python test files (named test_*.py) are there? Reply with just the number.",
            files={"tests/test_a.py": "", "tests/test_b.py": "", "src/mod.py": "", "src/test_c.py": ""},
            script=[Call("glob", {"pattern": "**/test_*.py"}), Say("3")],
            graders=[g.finished_ok(), g.reply_matches(r"\b3\b"), g.used_tool("glob")],
        ),
        Task(
            id="plan_with_todos", category="planning", title="Keep a task list for a multi-step job",
            prompt="Create a.txt containing 'A', b.txt containing 'B' and c.txt containing 'C'. Track your steps.",
            script=[Call("todo_write", {"todos": [{"content": "a.txt", "status": "in_progress"},
                                                  {"content": "b.txt"}, {"content": "c.txt"}]}),
                    Call("write_file", {"path": "a.txt", "content": "A"}),
                    Call("write_file", {"path": "b.txt", "content": "B"}),
                    Call("write_file", {"path": "c.txt", "content": "C"}),
                    Call("todo_write", {"todos": [{"content": "a.txt", "status": "completed"},
                                                  {"content": "b.txt", "status": "completed"},
                                                  {"content": "c.txt", "status": "completed"}]}),
                    Say("All three files created.")],
            graders=[g.finished_ok(), g.file_equals("a.txt", "A"), g.file_equals("b.txt", "B"), g.file_equals("c.txt", "C"),
                     g.used_tool("todo_write", at_least=2)],
        ),
        Task(
            id="background_job", category="shell", title="Start a long-running process, read it, stop it",
            prompt="Start `python server.py` in the background, confirm it prints 'server ready', then stop it.",
            files={"server.py": _JOB},
            script=[Call("run_shell", {"command": f"{_PY} server.py", "background": True}),
                    Call("run_shell", {"command": f'{_PY} -c "import time; time.sleep(1.5)"'}),
                    Call("shell_output", {"id": "job1"}),
                    Call("shell_kill", {"id": "job1"}),
                    Say("The server printed 'server ready' and I stopped it.")],
            graders=[g.finished_ok(), g.used_tool("shell_output"), g.used_tool("shell_kill"), g.reply_matches("server ready")],
        ),
        Task(
            id="big_output_is_kept", category="shell", title="Large command output is saved, not lost",
            prompt="Print the numbers 1 to 6000 (one per line) with a command, and tell me the last one.",
            script=[Call("run_shell", {"command": f'{_PY} -c "print(chr(10).join(str(i) for i in range(1, 6001)))"'}),
                    Say("6000")],
            graders=[g.finished_ok(), g.reply_matches(r"6000"), g.glob_exists(".abp-tool-output/run_shell-*.txt",
                                                                                label="the full output was saved")],
        ),
    ]
