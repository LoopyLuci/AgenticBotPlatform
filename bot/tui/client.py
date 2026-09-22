"""Re-exports bot/dashboard_client.py's DashboardClient - kept as its own module so nothing
that already imports `bot.tui.client` needs to change. The real implementation moved there
when abp_cli/ needed the same client (and more of it): see that module's own docstring.
"""

from __future__ import annotations

from bot.dashboard_client import ApiError, DashboardClient, default_connection

__all__ = ["ApiError", "DashboardClient", "default_connection"]
