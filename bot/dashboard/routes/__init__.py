"""The dashboard's HTTP routes, one module per area. Each module's register(app) adds its routes;
bot/dashboard/server.py's build_app() calls them in a fixed order (route order matters for matching)."""
