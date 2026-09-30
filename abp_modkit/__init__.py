"""abp_modkit: turn any project into an ABP module with next to no work.

    python -m abp_modkit adopt PATH          detect the project, write abp-module.toml + abp-ops.toml, register it
    python -m abp_modkit serve --spec abp-ops.toml --project PATH --home DIR     the module's control hub
    python -m abp_modkit mcp --home DIR      MCP over stdio for a running hub
    python -m abp_modkit check PATH          validate a project's module files, start its hub, list its operations

Standard library only, so a module needs nothing installed to be driven by ABP. See docs/modules/modkit.md.
"""
__version__ = "1.0.0"
