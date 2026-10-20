"""Autonomy 0-5 for a Local Chat session (PLAN-local-chat.md P4): how much the agent may do before it asks, as ABP's own
permission mode plus run-level rules (bot/agent_runtime/permissions.py). It only ever tightens or opens what the permission
system already decides: host rules still apply, admin tools always ask, a host-locked config accepts only its deny / ask
rules, and destructive or irreversible commands always ask - at every level. Pushing never happens from a chat turn: it goes
through the conductor's push queue.

    0 Locked       every tool call asks, reads included
    1 Cautious     reads run; every edit and command asks
    2 Supervised   ABP's defaults: each tool's own approval setting (edits ask)
    3 Trusted      edits inside the workspace run; commands still ask
    4 Autonomous   edits run, and the usual build / test / git-inspection commands
    5 Sovereign    edits and any single command run (a compound command still asks); long tasks unattended in the sandbox
"""
from __future__ import annotations

from typing import Any

LABELS = ("Locked", "Cautious", "Supervised", "Trusted", "Autonomous", "Sovereign")
SUMMARIES = ("every tool call asks, reads included", "reads run; every edit and command asks",
             "each tool's own approval setting (edits ask)", "edits inside the workspace run; commands still ask",
             "edits run, and the usual build / test / git-inspection commands",
             "edits and any single command run (a compound command still asks)")

# Always ask (every level): commands that destroy work or cannot be undone. Never: pushing (the push queue does that).
ALWAYS_ASK = ("rm *", "rmdir *", "del *", "erase *", "rd *", "Remove-Item*", "git reset --hard*", "git clean*", "git checkout -- *",
              "git restore *", "git branch -D*", "git branch -d*", "git stash drop*", "git stash clear*", "git rebase*",
              "git worktree remove*", "format *", "shutdown*", "Stop-Computer*", "Restart-Computer*", "reg delete*", "diskpart*",
              "mkfs*", "dd *", "chmod -R*", "chown -R*", "npm publish*", "pip uninstall*")
NEVER = ("git push*",)
SAFE_COMMANDS = ("git status*", "git diff*", "git log*", "git show*", "git branch", "git branch --list*", "python -m pytest*",
                 "pytest*", "python -m ruff*", "ruff *", "npm test*", "npm run test*", "npm run lint*", "npm run build*",
                 "cargo test*", "cargo build*", "cargo check*", "cargo clippy*", "go test*", "go build*", "dotnet test*",
                 "dotnet build*", "node --check*", "tsc*")


def policy(level: int) -> dict[str, Any]:
    """{"level", "label", "permission_mode", "rules": [raw rule dicts], "summary"} for a turn's context."""
    level = max(0, min(5, int(level)))
    rules: list[dict] = [{"decision": "deny", "tool": "run_shell", "match": m, "note": "pushing goes through the push queue, "
                          "never from a chat turn"} for m in NEVER]
    rules += [{"decision": "ask", "tool": "run_shell", "match": m, "note": "destructive or irreversible: always asks"}
              for m in ALWAYS_ASK]
    mode = "default"
    if level == 0:
        rules.append({"decision": "ask", "tool": "*", "note": "autonomy 0 (Locked): every call asks"})
    elif level == 1:
        rules += [{"decision": "ask", "tool": "class:write", "note": "autonomy 1 (Cautious): edits ask"},
                  {"decision": "ask", "tool": "run_shell", "note": "autonomy 1 (Cautious): commands ask"}]
    elif level == 3:
        mode = "accept_edits"
    elif level == 4:
        mode = "accept_edits"
        rules += [{"decision": "allow", "tool": "run_shell", "match": m} for m in SAFE_COMMANDS]
    elif level == 5:
        mode = "accept_edits"
        rules.append({"decision": "allow", "tool": "run_shell", "match": "*"})
    return {"level": level, "label": LABELS[level], "permission_mode": mode, "rules": rules, "summary": SUMMARIES[level]}
