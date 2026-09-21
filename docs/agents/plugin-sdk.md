# Plugin SDK

A plugin is a Python file on this machine that registers agent tools and/or slash commands. It is trusted local code (see
[ADR 0007](../adr/0007-plugins-are-trusted-local-code.md)): it runs in-process with the app's own privileges, exactly as
sensitive as handing someone shell access. It is not a sandbox and not a marketplace.

```python
# my_plugin.py
REQUIRES_SDK = ">=1.0,<2"          # optional: which SDK versions this was written for
PLUGIN_VERSION = "1.0.0"           # optional: shown in the plugin list
PLUGIN_DESCRIPTION = "Adds a word-count tool."

def setup(api):
    async def word_count(tool_input, *, workspace, instance_id):
        return str(len(tool_input.get("text", "").split()))

    api.register_tool("word_count", "Count the words in some text.",
                      {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, word_count)
```

Install with `/plugin install <path>` (or the dashboard / `create_plugin` tool); `setup()` raising aborts the install and leaves
nothing registered.

## The API and its version

`PluginAPI.register_tool(name, description, input_schema, handler, dangerous=False)` and
`PluginAPI.register_command(name, description, handler, category="Plugins", args_hint="", aliases=())`. Tool handlers are
`async def handler(tool_input: dict, *, workspace: Path, instance_id: Optional[int]) -> str`; command handlers are
`async def handler(ctx: CmdContext, args: list[str]) -> str`.

The SDK has a semantic version, `bot.plugins.SDK_VERSION` (**1.0**):

* a change that could break an existing plugin (a handler argument removed or renamed, a registration call changed) bumps the
  **major** number, and an old plugin is then refused with a message rather than failing strangely;
* an addition that old plugins can ignore bumps the **minor** number;
* `REQUIRES_SDK` clauses are joined by commas and use `>=`, `>`, `<=`, `<`, `==`, `!=`. Versions compare as numbers
  (`1.10` is newer than `1.9`). A requirement that cannot be read refuses the plugin;
* a plugin with no `REQUIRES_SDK` is assumed to want the 1.x line it was written against.

Plugin tools are treated like any other tool by the permission layer: `dangerous=True` makes the agent ask before each use.
