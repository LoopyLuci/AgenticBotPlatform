"""Domain storage functions, one module per area. Public API is still `bot.db` (which re-exports all of
these); these modules exist so no single file holds every table. Each function reaches shared state
(the connection, the write lock, other storage functions) through the `bot.db` module at call time."""
