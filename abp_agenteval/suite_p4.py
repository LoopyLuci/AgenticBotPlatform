"""Seed tasks for skill packs and named agents (roadmap P4)."""
from __future__ import annotations

from . import graders as g
from .task import Call, Say, Task

_SKILL = ("---\nname: release-notes\ndescription: Use when asked to write release notes - the house format and what to leave out.\n---\n"
          "Write release notes as one line per change, newest first, each starting with a verb in the past tense.\n"
          "Save them to RELEASE.md. Never list internal refactors.\n")


def p4_suite() -> list[Task]:
    return [
        Task(
            id="follow_a_skill_pack", category="skills", title="Load a skill pack and follow its house format",
            prompt="Write the release notes for this version into RELEASE.md. Changes: added dark mode; fixed the login crash.",
            files={".claude/skills/release-notes/SKILL.md": _SKILL},
            script=[Call("read_skill", {"name": "release-notes"}),
                    Call("write_file", {"path": "RELEASE.md", "content": "Fixed the login crash.\nAdded dark mode.\n"}),
                    Say("Wrote RELEASE.md.")],
            graders=[g.finished_ok(), g.used_tool("read_skill"), g.file_equals("RELEASE.md", "Fixed the login crash.\nAdded dark mode.\n"),
                     g.within_iterations(5)],
        ),
        Task(
            id="list_agents_then_delegate_read_only", category="skills", title="See the available agents before delegating",
            prompt="Which named agents can you delegate to? Reply with their names, comma separated.",
            script=[Call("list_agents", {}), Say("explore, plan, reviewer, general")],
            graders=[g.finished_ok(), g.used_tool("list_agents"), g.reply_matches(r"explore"), g.reply_matches(r"reviewer"),
                     g.did_not_use_tool("spawn_subagent")],
        ),
    ]
