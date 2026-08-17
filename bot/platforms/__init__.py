"""Optional messaging platforms beyond Telegram.

Each module here is a self-contained adapter: it owns its own client
library, its own allowlist check, and translates that platform's messages
into the same bot.router.ask() / bot.db.log_message() pipeline Telegram
already uses — so the dashboard's Chat view and Jobs table work uniformly
regardless of which platform a message came in on. bot/main.py starts
whichever platforms have credentials configured and skips the rest; none
of them (including Telegram) is mandatory on its own, only "at least one"
is, checked in main.py.
"""
