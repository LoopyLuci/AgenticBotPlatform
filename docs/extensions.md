# Extensions: what a local install adds on top of the public ABP

ABP's repository is public. It holds nothing private or specific to one machine: no absolute
paths, no private project integrations, no overlay for somebody's own checkout. Everything a
particular install needs on top of that lives in **one folder outside the checkout**, named by the
`ABP_PRIVATE_DIR` environment variable:

    <ABP_PRIVATE_DIR>/
        config/<name>.yaml    deep-merged over the tracked config/<name>.yaml
        catalog/<id>/         one more module-overlay root, next to catalog/
        extensions/<name>/    a Python package with register(api)
        denylist.txt          terms that must never reach a public repo

With `ABP_PRIVATE_DIR` unset, missing, or naming something that is not a folder, ABP behaves
exactly like a plain public install: one environment-variable lookup, an empty answer, and no
other code runs. That is the default, and it is what every test in this repo runs as unless it
says otherwise.

The point of the folder is that the tracked config keeps **generic defaults only** — a machine's
paths, its project list and its private integrations live in the overlay, where they are not
published, and where they can be edited without a pull request to a public repo.

- [Config overlays](#config-overlays)
- [Module overlays](#module-overlays)
- [Writing an extension](#writing-an-extension)
- [The extension API](#the-extension-api)
- [When an extension fails](#when-an-extension-fails)
- [Keeping a public repo public](#keeping-a-public-repo-public)

## Config overlays

Every `<ABP_PRIVATE_DIR>/config/<name>.yaml` is deep-merged over the tracked `config/<name>.yaml`
when that file is read (`bot/extensions.py:merge_overlay`). Three rules, and only three:

| In the overlay | Effect |
|---|---|
| a **mapping** | merged key by key, recursively |
| a **list** | **replaces** the tracked list — unless the key ends in `+` (`security.allowed_origins+:`), which **appends** |
| `null` | **deletes** the key, which is how a private folder turns off a tracked default |

Anything that is not a mapping at the top level is replaced outright: a partial list of backends is
a list of backends, not a patch.

Replacing rather than appending is the default on purpose. A list in this config usually grants
something (`allowed_origins`, `overlay_dirs`, `search_paths`), and silently appending to a grant
would make the overlay weaker to write and harder to reason about than the file it overrides. When
you do want to add to a list, say so in the key.

The merge is pure — a dict in, a dict out — so it is testable on its own and needs no ABP running.

Only one overlay is worth writing by hand: `backends.yaml`. It is where the machine's paths go
(`modules.build_cache`, `modules.projects`, a resolver address), and it is the file a tracked
default must stay generic in.

While a `backends.yaml` overlay exists, a setting saved through ABP (a Settings page, a learned
wake-on-LAN address, a linked backend) is written into the overlay, not the tracked file: the
overlay wins the merge, so an edit under it would never take effect, and the machine's own settings
must not end up in the checkout. A hand edit of the overlay hot-reloads like one of the tracked
file, and Config history keeps the overlay's versions as `private-backends.yaml`.

## Module overlays

`<ABP_PRIVATE_DIR>/catalog/` is one more overlay root, listed by `bot/modules/registry.py:overlay_roots()`
next to the tracked `catalog/` and the user root `data/module-overlays/`. First match wins, so an
overlay in the private folder can be shadowed by one in the tracked `catalog/` of the same id —
which is the right way round: a private folder refines a public overlay, it does not fight it.

An overlay is exactly what it is in `catalog/`: an `abp-module.toml` and, if it drives operations,
an `abp-ops.toml`. Nothing about the mechanism changes; only where the folder is.

## Writing an extension

An extension is a folder under `<ABP_PRIVATE_DIR>/extensions/` with two things in it: a manifest
and a Python package that exposes `register(api)`.

    <ABP_PRIVATE_DIR>/extensions/hello/
        abp_extension.toml
        ext_hello/__init__.py      <- the `entry` module, imported by file location
        panel.js                    <- a UI panel, served from this folder, never copied into ABP

`abp_extension.toml`:

    [extension]
    name = "hello"
    version = "1.0.0"
    description = "What this extension is, in one sentence a person will read in the modules view."
    entry = "ext_hello"           # the package to import; its register(api) runs
    minimum_abp = "0.7.0"         # the oldest ABP this works with

Everything in `[extension]` is required except `minimum_abp`, and a missing field is an error that
names it: a silently half-loaded extension is worse than one that reports itself broken.

`register(api)` is called once, at load time. It should register and return — no long-running work,
no threads, no network. Work belongs behind the routes and tools it registers.

The entry module is imported **by file location**, not through `sys.path`: nothing in a private
folder is importable by accident, and a private folder must never shadow one of ABP's own modules.

## The extension API

`register(api)` receives an `ExtensionAPI` (`bot/extensions.py`). Every method is a registration
call; nothing reaches into ABP's internals, and an extension that registers nothing is simply
invisible.

| Call | What it adds |
|---|---|
| `api.add_backend(name, **settings)` | a backend under `name`, in the same `backends:` section the tracked config uses |
| `api.add_integration(name, **settings)` | an entry in `integrations:` — the section `bot/integrations.py` reads |
| `api.add_routes(register)` | dashboard API routes: `register(app, read_auth, write_auth)`, added exactly like one of ABP's own route modules |
| `api.add_panel(id, title, *, description, script, styles, icon)` | a UI panel in both the dashboard and the desktop app, served from this folder |
| `api.add_cli_command(name, run, *, help, add_arguments)` | an `abp_cli` command: `await run(args, client) -> int` |
| `api.add_mcp_tool(func, name=None)` | an MCP tool on `bot/mcp_server.py`'s server; the docstring is the tool's description |
| `api.add_module(entry)` | a module entry (one item of `modules.extra`'s shape), so a private project is a module |
| `api.add_test_dir(path)` | tests the local pipeline runs when `ABP_PRIVATE_DIR` is set |

Backends and integrations are merged into the effective config as one document, so an extension
can set a value the private overlay would otherwise have to repeat. Everything else is applied where
it belongs: routes when the dashboard's app is built, panels when a UI loads, the CLI when its
parser is built, MCP tools when `bot.mcp_server` is imported, module entries on the registry's next
read (seconds, not a restart).

### Routes

`api.add_routes(register)` takes a function with the same signature as one of ABP's own route
modules, so the auth story is ABP's, not the extension's:

    def add_routes(app, read_auth, write_auth):
        from fastapi import Depends, HTTPException

        @app.get("/api/hello", dependencies=[Depends(read_auth)])
        async def hello():
            return {"greeting": "hello"}

        @app.post("/api/hello/send", dependencies=[Depends(write_auth)])
        async def hello_send(payload: dict):
            ...

`read_auth` is the dashboard's normal read token; `write_auth` is the stricter write token. An
extension that needs neither can ignore both arguments.

### Panels

A panel is one `panel.js` in the extension's own folder. Nothing about it is copied into the repo:
the server serves it from `/extensions/<name>/...`, and `bot/dashboard/static/extensions.js` — the
same file in each UI's own tree, as every shared UI script here is (`desktop-app/ui/extensions.js`
for the desktop shell) — asks `/api/extensions` what exists and builds the nav entry, the section
and the `<script>` tag at runtime.

A panel script follows the same contract as a panel ABP ships: an IIFE that takes the page's `api`,
draws into `#<panel_id>-root`, and reloads on its own `hashchange`. `tests/fixtures/ext_hello/panel.js`
is a complete, working example.

Because the nav is built at runtime, an extension panel works in the browser dashboard and in the
desktop app without either knowing about it — and disappears from both the moment the folder goes
away.

### CLI commands

    async def hello_cmd(args, client) -> int:
        print("hello from an extension")
        return 0

    api.add_cli_command("hello", hello_cmd, help="say hello", add_arguments=lambda p: p.add_argument("--loud", action="store_true"))

`run` is awaited exactly like one of ABP's own handlers, with the same `DashboardClient`. A command
whose name ABP already owns is skipped with a note rather than shadowing it, so an extension can
never break the built-in CLI by accident.

### Test directories

`api.add_test_dir(path)` — a relative path is resolved against the extension's own folder.
`scripts/local_pipeline.py` runs each existing one as part of its `extension-tests` step, and skips
the step entirely when no extension declared any. This is how a private integration gets tested by
the same pipeline that tests ABP, without a line of it being committed here.

## When an extension fails

**A failing extension never breaks ABP.** Each one is loaded in isolation: a missing field, an
import that raises, a `register()` that raises, or a `minimum_abp` newer than the running ABP are
all recorded and skipped, and ABP carries on without it. The exception is logged with its traceback,
the extension is reported as failed through `GET /api/extensions` (the modules/extensions view), and
nothing already registered by an earlier, healthy extension is rolled back.

Failure is scoped as narrowly as the hook: an extension whose `register()` raises contributes
nothing at all, one whose route registration raises keeps its config, panels and tools, and one
whose panel script 404s still has its nav entry.

## Keeping a public repo public

`scripts/leak_guard.py` scans a public repo for the two things that break that, and it is wired in
twice: as a **pre-commit** hook (`scripts/install_git_hooks.sh` / `.ps1`, which also installs the
pre-push pipeline) on the lines the index is about to add, and as the pipeline's
**Public-repo leak guard** step over every tracked file.

- **machine paths** — an absolute drive-letter path, or a user profile carrying a real account
  name. Windows' own folders, a bare drive root and a placeholder account (`<you>`, `someone`) are
  not machine paths: every Windows machine has them, and a test of Windows path handling has to
  name them.
- **private terms** — whatever `denylist.txt` in the private folder lists, one term or regex per
  line. **The list itself never enters the public repo**; ABP only ever reads it from there.

Each category is `fail` or `warn`, set in `<ABP_PRIVATE_DIR>/config/leak_guard.yaml`, as is the
allowlist — so both the exceptions and the strictness live with the private folder, never here:

    modes:
      machine_paths: fail
      private_terms: fail
    allow:
      - docs/agents/ROADMAP.md

Generated and vendored trees (`requirements.lock`, the OpenAPI dump, bundled JS libraries) are not
scanned: they are written by a tool from somewhere else and never hand-edited here.

Run it by hand any time:

    python scripts/leak_guard.py              # every tracked file
    python scripts/leak_guard.py --staged     # what the index is about to record