"""Hand-authored training phrases for the Support Bot's intent classifier.

Every management action BotServer exposes gets its own intent with a spread
of natural phrasings — this is the entire "training set" the pure-Python
TF-IDF/centroid model in model.py learns from at import time. Add more
phrasings here (not code) to improve recognition for a given intent.
"""

from __future__ import annotations

EXAMPLES: list[tuple[str, str]] = [
    # status
    ("what's the server status", "status"),
    ("how's everything running", "status"),
    ("give me a status update", "status"),
    ("is the server healthy", "status"),
    ("show me the overview", "status"),
    ("how many jobs are running", "status"),
    ("what's the success rate", "status"),
    ("server health check", "status"),
    ("status", "status"),
    ("how are things looking", "status"),

    # list_bots
    ("list my bots", "list_bots"),
    ("show all bot instances", "list_bots"),
    ("what bots do i have", "list_bots"),
    ("show me the bots", "list_bots"),
    ("which bots are running", "list_bots"),
    ("list all my telegram discord slack bots", "list_bots"),
    ("show bot instances", "list_bots"),

    # bot_create
    ("create a new bot", "bot_create"),
    ("add a new discord bot", "bot_create"),
    ("set up a new telegram bot instance", "bot_create"),
    ("i want to add a bot", "bot_create"),
    ("register a new slack bot", "bot_create"),

    # bot_edit
    ("edit my telegram bot", "bot_edit"),
    ("change the backend for bot X", "bot_edit"),
    ("update the settings for a bot instance", "bot_edit"),
    ("rename bot X", "bot_edit"),
    ("change the model for a bot", "bot_edit"),

    # bot_delete
    ("delete the discord bot", "bot_delete"),
    ("remove bot instance X", "bot_delete"),
    ("get rid of my slack bot", "bot_delete"),
    ("delete bot X", "bot_delete"),
    ("permanently remove a bot instance", "bot_delete"),

    # bot_enable
    ("enable the telegram bot", "bot_enable"),
    ("turn on bot X", "bot_enable"),
    ("re-enable my discord bot", "bot_enable"),
    ("activate bot instance X", "bot_enable"),

    # bot_disable
    ("disable the slack bot", "bot_disable"),
    ("turn off bot X", "bot_disable"),
    ("deactivate my telegram bot", "bot_disable"),
    ("stop bot X from responding", "bot_disable"),

    # bot_restart
    ("restart bot X", "bot_restart"),
    ("restart my discord bot", "bot_restart"),
    ("bounce the telegram bot", "bot_restart"),
    ("restart the slack bot instance", "bot_restart"),
    ("kick the bot process and start it again", "bot_restart"),

    # backend_show
    ("what backend am i using", "backend_show"),
    ("show the current backend settings", "backend_show"),
    ("what's the default backend", "backend_show"),
    ("show routing config", "backend_show"),
    ("which backend handles quick questions", "backend_show"),
    ("what backend is set as default", "backend_show"),
    ("which backend is currently active", "backend_show"),

    # backend_set
    ("set the default backend to cli", "backend_set"),
    ("switch to the api backend", "backend_set"),
    ("use hermes cli for quick questions", "backend_set"),
    ("change default backend to hermes_gateway", "backend_set"),
    ("route everything through the ui backend", "backend_set"),

    # model_show
    ("what model am i using", "model_show"),
    ("show the current models", "model_show"),
    ("which model is the api backend using", "model_show"),
    ("list the models in use", "model_show"),

    # model_set
    ("set the api model to opus", "model_set"),
    ("switch the model to claude sonnet 5", "model_set"),
    ("use claude opus 5 for api requests", "model_set"),
    ("change hermes cli model to something else", "model_set"),
    ("set model for hermes gateway", "model_set"),

    # mcp_list
    ("list mcp servers", "mcp_list"),
    ("show me the mcp servers", "mcp_list"),
    ("what mcp servers are configured", "mcp_list"),
    ("which mcp servers are enabled", "mcp_list"),

    # mcp_enable
    ("enable the filesystem mcp server", "mcp_enable"),
    ("turn on the github mcp server", "mcp_enable"),
    ("enable mcp server X", "mcp_enable"),

    # mcp_disable
    ("disable the filesystem mcp server", "mcp_disable"),
    ("turn off mcp server X", "mcp_disable"),
    ("disable an mcp server", "mcp_disable"),

    # mcp_logs
    ("show me the logs for the filesystem mcp server", "mcp_logs"),
    ("tail the mcp logs for server X", "mcp_logs"),
    ("what's in the log for mcp server X", "mcp_logs"),

    # desktop_start
    ("start claude desktop", "desktop_start"),
    ("launch claude desktop", "desktop_start"),
    ("open claude desktop", "desktop_start"),
    ("boot up claude desktop", "desktop_start"),

    # desktop_stop
    ("stop claude desktop", "desktop_stop"),
    ("shut down claude desktop", "desktop_stop"),
    ("close claude desktop", "desktop_stop"),
    ("kill claude desktop", "desktop_stop"),

    # desktop_restart
    ("restart claude desktop", "desktop_restart"),
    ("bounce claude desktop", "desktop_restart"),
    ("reboot claude desktop", "desktop_restart"),

    # config_reload
    ("reload the config", "config_reload"),
    ("reload config from disk", "config_reload"),
    ("refresh the configuration", "config_reload"),
    ("pick up config changes from the file", "config_reload"),

    # allowed_users_list
    ("who's allowed to use the bot", "allowed_users_list"),
    ("list allowed users", "allowed_users_list"),
    ("show the allowlist", "allowed_users_list"),
    ("which telegram users can talk to the bot", "allowed_users_list"),

    # help
    ("help", "help"),
    ("what can you do", "help"),
    ("what commands do you support", "help"),
    ("how do i use you", "help"),
    ("list your capabilities", "help"),
]

# Intents whose action changes state in a way that's disruptive or hard to
# undo — these route through the confirm flow (bot/support_bot/engine.py)
# instead of executing immediately, mirroring cmd_desktop's confirm gate
# in bot/commands.py and the "security.confirm_destructive" config flag.
DESTRUCTIVE_INTENTS = {
    "bot_delete",
    "bot_disable",
    "desktop_stop",
    "desktop_restart",
    "mcp_disable",
    "bot_restart",
}
