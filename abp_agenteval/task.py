"""Task definitions and the small vocabulary of steps a scripted run replays."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Say:
    """The scripted model replies with text and stops."""
    text: str


@dataclass(frozen=True)
class Call:
    """The scripted model asks for one or more tool calls in a single turn."""
    tool: str
    args: dict = field(default_factory=dict)
    more: tuple = ()          # further (tool, args) pairs in the same turn


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    category: str = "general"
    files: dict[str, str] = field(default_factory=dict)          # workspace fixture, relative path -> text
    outside_files: dict[str, str] = field(default_factory=dict)  # files placed beside the workspace (for boundary tasks)
    script: list = field(default_factory=list)                   # golden trajectory for scripted mode
    graders: list[Callable[["Context"], "Check"]] = field(default_factory=list)
    approvals: dict[str, str] = field(default_factory=dict)      # tool -> "deny"; anything else is approved
    max_iterations: int = 20
    tags: tuple = ()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Context:
    """What a grader may look at."""
    workspace: Any            # pathlib.Path of the throwaway workspace
    outside: Any              # pathlib.Path beside it (boundary tasks)
    reply: str
    trace: dict               # bot.agent_runtime.trace.summarize(...)
    error: Optional[str] = None
