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

    # jobs_list
    ("list recent jobs", "jobs_list"),
    ("show me the job queue", "jobs_list"),
    ("what jobs failed today", "jobs_list"),
    ("show running jobs", "jobs_list"),
    ("list failed jobs", "jobs_list"),
    ("show job history", "jobs_list"),
    ("what jobs are queued", "jobs_list"),

    # job_status
    ("what's the status of job 42", "job_status"),
    ("check job #17", "job_status"),
    ("show me job 5", "job_status"),
    ("did job 12 succeed", "job_status"),

    # swarms_list
    ("list my swarms", "swarms_list"),
    ("show all swarms", "swarms_list"),
    ("what swarms do i have", "swarms_list"),
    ("which swarms are configured", "swarms_list"),

    # swarm_run
    ("run the research swarm", "swarm_run"),
    ("trigger swarm X with prompt hello", "swarm_run"),
    ("start a run of swarm X", "swarm_run"),
    ("kick off the fanout swarm", "swarm_run"),

    # swarm_run_status
    ("check the status of the last swarm run", "swarm_run_status"),
    ("how's the swarm run going", "swarm_run_status"),
    ("what happened in swarm run X", "swarm_run_status"),
    ("show me the swarm run result", "swarm_run_status"),

    # diagnostics
    ("show me the error rate", "diagnostics"),
    ("what's the latency by backend", "diagnostics"),
    ("show recent errors", "diagnostics"),
    ("how's the connection health", "diagnostics"),
    ("any errors in the last few minutes", "diagnostics"),
    ("run diagnostics", "diagnostics"),

    # db_status
    ("how big is the database", "db_status"),
    ("show database size", "db_status"),
    ("what's the db table row counts", "db_status"),
    ("database status", "db_status"),

    # db_vacuum
    ("vacuum the database", "db_vacuum"),
    ("reclaim space in the db", "db_vacuum"),
    ("run vacuum", "db_vacuum"),
    ("clean up the database", "db_vacuum"),

    # backups_list
    ("list backups", "backups_list"),
    ("show env backups", "backups_list"),
    ("what backups are available", "backups_list"),
    ("list bot instance backups", "backups_list"),

    # backup_restore
    ("restore the latest backup", "backup_restore"),
    ("restore backup env-20260101", "backup_restore"),
    ("roll back to a previous backup", "backup_restore"),
    ("restore bot instances from backup", "backup_restore"),

    # settings_show
    ("show feature toggles", "settings_show"),
    ("what's the agent control mode", "settings_show"),
    ("show current settings", "settings_show"),
    ("is ui automation enabled", "settings_show"),
    ("show security settings", "settings_show"),

    # settings_set
    ("turn on verbose telemetry", "settings_set"),
    ("disable ui automation", "settings_set"),
    ("enable confirm destructive actions", "settings_set"),
    ("set agent control mode to allowlist", "settings_set"),
    ("turn off confirm destructive", "settings_set"),
    ("switch agent control to trust all", "settings_set"),

    # devices_list
    ("list paired devices", "devices_list"),
    ("show my mobile devices", "devices_list"),
    ("which devices are paired", "devices_list"),
    ("what phones are connected", "devices_list"),

    # device_revoke
    ("revoke my old phone", "device_revoke"),
    ("remove the paired device X", "device_revoke"),
    ("unpair device X", "device_revoke"),
    ("revoke device X's key", "device_revoke"),

    # device_retier — Admin control surface plan, Section 4 expansion
    ("set device X's permission tier to elevated", "device_retier"),
    ("change my tablet's tier to standard", "device_retier"),
    ("grant device X unrestricted access", "device_retier"),
    ("retier the kindle fire to none", "device_retier"),
    ("give my other phone elevated permissions", "device_retier"),

    # mobile_key_create
    ("generate a new pairing key", "mobile_key_create"),
    ("create a mobile pairing key", "mobile_key_create"),
    ("i need a new pairing key for my tablet", "mobile_key_create"),
    ("pair a new device", "mobile_key_create"),
    ("pair a new device at elevated tier", "mobile_key_create"),
    ("create a pairing key with standard permissions", "mobile_key_create"),

    # app_update
    ("update the app", "app_update"),
    ("update my phone", "app_update"),
    ("push the latest apk to my phone", "app_update"),
    ("update the android app", "app_update"),
    ("send the latest build to my device", "app_update"),
    ("install the newest version on my phone", "app_update"),
    ("update my xiaomi", "app_update"),
    ("can you update the mobile app for me", "app_update"),
    ("push the new apk", "app_update"),
    ("update all paired devices", "app_update"),

    # sessions_list
    ("list recent sessions", "sessions_list"),
    ("show my sessions", "sessions_list"),
    ("search sessions for X", "sessions_list"),
    ("what conversations happened today", "sessions_list"),

    # session_show
    ("show me session 5", "session_show"),
    ("what's in session 12", "session_show"),
    ("open session #3", "session_show"),

    # claude_setup_check
    ("check claude desktop setup", "claude_setup_check"),
    ("is claude desktop ready", "claude_setup_check"),
    ("verify claude desktop connection", "claude_setup_check"),
    ("help me connect claude desktop", "claude_setup_check"),
    ("is the ui backend set up", "claude_setup_check"),

    # hermes_setup_check
    ("check hermes setup", "hermes_setup_check"),
    ("is hermes ready", "hermes_setup_check"),
    ("verify hermes connection", "hermes_setup_check"),
    ("help me connect hermes agent", "hermes_setup_check"),
    ("is the hermes gateway backend set up", "hermes_setup_check"),
    ("check for hermes telegram token conflicts", "hermes_setup_check"),

    # help
    ("help", "help"),
    ("what can you do", "help"),
    ("what commands do you support", "help"),
    ("how do i use you", "help"),
    ("list your capabilities", "help"),

    # estop_status — Admin control surface plan, Section 4
    ("is the emergency stop engaged", "estop_status"),
    ("check estop status", "estop_status"),
    ("is everything halted", "estop_status"),
    ("emergency stop status", "estop_status"),

    # estop_engage
    ("engage the emergency stop", "estop_engage"),
    ("halt everything now", "estop_engage"),
    ("emergency stop the whole system", "estop_engage"),
    ("stop all new work immediately", "estop_engage"),
    ("activate the kill switch", "estop_engage"),

    # estop_disengage
    ("disengage the emergency stop", "estop_disengage"),
    ("resume normal operation", "estop_disengage"),
    ("turn off the emergency stop", "estop_disengage"),
    ("let work start again", "estop_disengage"),
    ("clear the kill switch", "estop_disengage"),

    # hooks_list — Admin control surface plan, Section 4 expansion
    ("list hooks", "hooks_list"),
    ("show configured hooks", "hooks_list"),
    ("what hooks are set up", "hooks_list"),
    ("list agent hooks", "hooks_list"),

    # hook_enable
    ("enable hook 3", "hook_enable"),
    ("turn on hook number 2", "hook_enable"),
    ("re-enable hook 5", "hook_enable"),

    # hook_disable
    ("disable hook 3", "hook_disable"),
    ("turn off hook number 2", "hook_disable"),
    ("pause hook 5", "hook_disable"),

    # hook_remove
    ("remove hook 3", "hook_remove"),
    ("delete hook number 2", "hook_remove"),
    ("get rid of hook 5", "hook_remove"),

    # agent_settings_show
    ("show agent settings for BotServer Control", "agent_settings_show"),
    ("what are the agent settings for X", "agent_settings_show"),
    ("show me X's agent config", "agent_settings_show"),

    # agent_settings_set_effort
    ("set X's worker effort to high", "agent_settings_set_effort"),
    ("change the manager effort for BotServer Control to max", "agent_settings_set_effort"),
    ("set X's effort level to medium", "agent_settings_set_effort"),

    # auto_manage_show
    ("show auto-manage settings for X", "auto_manage_show"),
    ("is auto-manage on for BotServer Control", "auto_manage_show"),
    ("what's the auto-manage config for X", "auto_manage_show"),
]

# Admin control surface plan, Section 4 — intents that reach the same
# admin_* capability the Telegram admin bot's tool loop gets
# (bot/agent_runtime/tools.py's ADMIN_TOOLS_*), gated here by the
# CALLING DEVICE's own permission_tier (bot/device_tiers.py) rather than
# an is_admin_instance flag, since Support Bot has no bot_instances
# concept of its own to be "the admin instance." Maps intent -> the
# minimum tier required; an intent absent from this dict has no
# tier requirement (every pre-existing intent keeps working exactly as
# it always has for any authenticated caller, unchanged by this plan).
ADMIN_ONLY_INTENTS: dict[str, str] = {
    "estop_status": "standard",
    "estop_engage": "elevated",
    "estop_disengage": "elevated",
    # devices_list/device_revoke predate device_tiers.py and were
    # unguarded until now — closing that gap is the first fix under the
    # "Admin control surface" plan's Section 4 expansion, independent of
    # the rest of it. Revoking is the same "manage a lower-tier device"
    # action bot/dashboard/server.py's own device-callable revoke route
    # gates at elevated-and-can_manage; listing devices is read-only,
    # gated at standard purely to keep pairing metadata away from
    # completely unprivileged (tier "none") devices.
    "devices_list": "standard",
    "device_revoke": "elevated",
    "device_retier": "elevated",
    "hooks_list": "standard",
    "hook_enable": "elevated",
    "hook_disable": "elevated",
    "hook_remove": "elevated",
    "agent_settings_show": "standard",
    "agent_settings_set_effort": "elevated",
    "auto_manage_show": "standard",
}

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
    "db_vacuum",
    "backup_restore",
    "device_revoke",
    "device_retier",
    "hook_disable",
    "hook_remove",
    "mobile_key_create",
    "estop_engage",
    "estop_disengage",
}
