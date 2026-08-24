"""The agent-loop engine — real multi-turn tool execution, approval
gating, and per-chat turn control (stop/queue/steer/pause/background),
built natively inside BotServer rather than delegated to an external
program. See engine.py, tools.py, and approval.py for the three pieces;
bot/backends/api_backend.py is the one backend that actually runs a tool
loop (the only backend where BotServer itself decides what to execute —
see that module's docstring for why the other backends only get
whole-request-granularity control from this package, not per-tool-call
control)."""
