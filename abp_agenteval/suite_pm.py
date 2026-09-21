"""Seed tasks for model knowledge and allowances (roadmap PM)."""
from __future__ import annotations

from . import graders as g
from .task import Call, Say, Task


def pm_suite() -> list[Task]:
    return [
        Task(
            id="know_your_allowance", category="models", title="Look up your own model's daily limit instead of guessing",
            prompt="How many requests per day is the model you are running on allowed? Reply with just the number.",
            config={"models": {"limits": {"*": {"rpd": 50, "rpm": 20}}}},
            script=[Call("model_info", {}), Say("50")],
            graders=[g.finished_ok(), g.used_tool("model_info"), g.reply_matches(r"\b50\b"), g.within_iterations(3)],
        ),
    ]
